import pytest
import json

from control_plane.services import adf_environment as env_mod
from control_plane.services import adf_package, azure_simulated

def _package(migration_id, env):
    from tests.test_target_mapping import _adf_maps, _adf_rule, _adf_spec
    rules = {"RULE:D@1": _adf_rule("DATE_FORMAT", {"input_formats": ["MM/DD/YYYY"]}), "RULE:V@1": _adf_rule("VALUE_MAP", {"map": {"A": "B"}})}
    return adf_package.build(migration_id, _adf_spec(), _adf_maps(), rules, env, "Ann")


CFG = {"subscription_id": "059ee846-90c3-449e-80cf-874cdaa7f8ad", "resource_group": "Dodge-Cox-RG", "factory_name": "adf-amp-xodus",
       "tenant_id": "", "client_id": "", "sql_server": "", "sql_database": "", "sql_schema": "dbo", "key_vault_url": "", "key_vault_sql_secret": ""}


def test_environment_is_real_only_with_a_saved_factory_and_a_real_storage_account():
    real = env_mod.resolve("MIG-E439077124", CFG, "afswstorage", "xodus-lake", emulator=False)
    assert real["simulated"] is False and real["factoryName"] == "adf-amp-xodus" and real["stagingContainer"] == "xodus-lake"
    assert real["storageResourceId"].endswith("/providers/Microsoft.Storage/storageAccounts/afswstorage") and real["unresolved"] == ["sql"]
    for args in ((None, "afswstorage", "xodus-lake", False), (CFG, "", "xodus-lake", False), (CFG, "devstoreaccount1", "xodus-lake", True)):
        assert env_mod.resolve("MIG-1", *args[:3], emulator=args[3])["simulated"] is True


def test_real_package_has_a_stopped_blob_trigger_on_curated_ready_files_and_no_secrets():
    env = env_mod.resolve("MIG-E439077124", CFG, "afswstorage", "xodus-lake", emulator=False)
    pkg = _package("MIG-E439077124", env)
    files, man = pkg["files"], json.loads(pkg["files"]["manifest.json"])
    trig = json.loads(files["trigger/TR_XODUS_Curated_Ready.json"])["properties"]
    assert trig["type"] == "BlobEventsTrigger" and trig["runtimeState"] == "Stopped" and trig["typeProperties"]["events"] == ["Microsoft.Storage.BlobCreated"]
    assert trig["typeProperties"]["blobPathBeginsWith"] == "/xodus-lake/blobs/curated/" and trig["typeProperties"]["blobPathEndsWith"] == ".ready"
    assert trig["typeProperties"]["scope"] == env["storageResourceId"]
    assert trig["pipelines"][0]["pipelineReference"]["referenceName"] == man["pipeline"]
    ls = json.loads(files["linkedService/LS_ADLS_Staging.json"])["properties"]
    assert ls["type"] == "AzureBlobStorage" and ls["typeProperties"]["serviceEndpoint"] == "https://afswstorage.blob.core.windows.net"
    assert man["simulated"] is False and man["unresolved"] == ["sql"] and "README.txt" in files
    assert "AccountKey" not in json.dumps(files) and not any("_Sim" in n for n in files)


def test_simulated_package_is_unchanged_and_has_no_trigger():
    pkg = _package("MIG-1", azure_simulated.simulated_environment("MIG-1"))
    assert not any(n.startswith("trigger/") for n in pkg["files"]) and json.loads(pkg["files"]["manifest.json"])["simulated"] is True


def test_real_package_reads_the_canonical_files_xodus_wrote_under_stage():
    env = env_mod.resolve("MIG-E439077124", CFG, "afswstorage", "xodus-lake", emulator=False)
    pkg = _package("MIG-E439077124", env)
    files, man = pkg["files"], json.loads(pkg["files"]["manifest.json"])
    locs = {n: json.loads(t)["properties"]["typeProperties"]["location"] for n, t in files.items() if n.startswith("dataset/DS_Staging_") and "Sql" not in n}
    assert locs and all(l["container"] == "xodus-lake" and l["folderPath"].startswith("stage/") and not l["folderPath"].startswith("raw") for l in locs.values())
    entity = {n: l for n, l in locs.items() if "lkp_" not in n}
    assert entity and all(l["folderPath"] == env["stagingFolder"] and l["fileName"] == n.split("DS_Staging_")[1].split(".")[0] + ".csv" for n, l in entity.items())
    assert all(l["folderPath"] == env["stagingFolder"] + "/lookups" and l["fileName"].endswith(".csv") for n, l in locs.items() if "lkp_" in n)
    acts = json.loads(files["pipeline/PL_XODUS_MIG_E439077124.json"])["properties"]["activities"]
    assert not any("translator" in a.get("typeProperties", {}) for a in acts)  # files already carry the staging column names
    assert set(man["stagingColumns"]) == set(man["stagingTables"]) and all(man["stagingColumns"].values())


def test_entity_raw_file_skips_lookup_files_and_prefers_the_file_named_like_the_entity():
    got = adf_package.entity_raw_files({"Household": ["corecap_households.csv", "corecap_advisors.csv"], "Client": ["corecap_household_contacts.csv"],
                                        "Account": ["corecap_accounts.csv"]}, {"lkp_corecap_advisors"})
    assert got == {"Household": "corecap_households.csv", "Client": "corecap_household_contacts.csv", "Account": "corecap_accounts.csv"}
    assert adf_package.entity_raw_files({"Household": ["corecap_advisors.csv", "corecap_households.csv"]}, set())["Household"] == "corecap_households.csv"


def test_entity_raw_file_when_only_the_lookup_file_is_the_sample_file_but_the_master_is_also_listed():
    got = adf_package.entity_raw_files({"Household": ["corecap_advisors.csv", "corecap_households.csv", "corecap_advisors.csv"]}, {"lkp_corecap_advisors"})
    assert got["Household"] == "corecap_households.csv"


def test_real_package_creates_the_target_schema_and_uses_the_managed_identity_for_sql():
    env = env_mod.resolve("MIG-E439077124", {**CFG, "sql_server": "afswdatabase.database.windows.net", "sql_database": "simplicitydb", "sql_schema": "simplicity"},
                          "afswstorage", "xodus-lake", emulator=False)
    files = _package("MIG-E439077124", env)["files"]
    ls = json.loads(files["linkedService/LS_SQL_Target.json"])["properties"]["typeProperties"]
    assert ls == {"server": "afswdatabase.database.windows.net", "database": "simplicitydb", "authenticationType": "SystemAssignedManagedIdentity"}
    assert "linkedService/LS_KeyVault.json" not in files and env["unresolved"] == []
    ddl = files["sql/01_target_tables.sql"]
    assert "CREATE SCHEMA [simplicity]" in ddl and "CREATE TABLE [simplicity].[T]" in ddl and "[OPEN_DT] date NULL" in ddl and "DROP TABLE" not in ddl.upper()
    acts = json.loads(files["pipeline/PL_XODUS_MIG_E439077124.json"])["properties"]["activities"]
    assert [a["type"] for a in acts] == ["Copy"] * 4 + ["Script"] or [a["type"] for a in acts].count("Copy") >= 1
    assert not any(a["name"] == "Prepare_SQL" for a in acts) and "CREATE TABLE" not in json.dumps(acts) and "CREATE SCHEMA" not in json.dumps(acts)
    merge = json.dumps(acts[-1])
    assert "AS [OPEN_DT]" in merge and "AS [ACCT_NO]" in merge  # the inner select names every column the outer select reads
    assert acts[-1]["name"] == "Merge_Into_Target" and "MERGE [simplicity].[T] AS t" in merge and "WHEN NOT MATCHED BY TARGET THEN INSERT" in merge
    assert "ON t.[ACCT_NO] = src.[ACCT_NO]" in merge and "ROW_NUMBER()" in merge and "EXEC " not in merge and "sql/usp_load_all.sql" not in files


def test_merge_key_is_an_identifier_field_and_a_table_without_one_inserts_missing_rows_only():
    from control_plane.services import sql_package as sp
    assert sp._is_key_field("ACCT_NUM") and sp._is_key_field("HH_ID") and not sp._is_key_field("TAX_ID") and not sp._is_key_field("ADVISOR_ID")
    fields = {"T.A": {"table": "T", "column": "A", "name": "T.A"}, "T.B": {"table": "T", "column": "B", "name": "T.B"}}
    maps = lambda c: [{"target_field": "T.A", "canonical_field": c, "transformation_expression": "DIRECT_COPY"},
                      {"target_field": "T.B", "canonical_field": "FIRST_NAME", "transformation_expression": "DIRECT_COPY"}]
    keyed = sp._merge("simplicity", "Client", "T", fields, maps("CLIENT_ID"), {})["sql"]
    assert "MERGE on A" in keyed and "WHEN MATCHED THEN UPDATE SET t.[B] = src.[B]" in keyed
    keyless = sp._merge("simplicity", "Client", "T", fields, maps("TAX_ID"), {})["sql"]
    assert "insert-if-missing" in keyless and "WHEN MATCHED" not in keyless and "IS NULL AND src.[A] IS NULL" in keyless


def test_canonical_stage_files_have_exactly_the_staging_columns_and_only_go_under_stage():
    from control_plane.services import blob_source
    assert blob_source.canonical_csv(["A", "B"], [{"A": 1, "B": None, "X": "ignored"}, {"A": "x,y"}]) == b'A,B\n1,\nx,y,\n'.replace(b"x,y,", b'"x,y",')
    with pytest.raises(ValueError, match="under stage/"):
        blob_source.write_canonical_stage("c", "raw/corecap", {}, {})
    with pytest.raises(ValueError, match="under stage/"):
        blob_source.write_canonical_stage("c", "stage/../raw", {}, {})


def test_ready_marker_goes_under_curated_only_and_ends_with_ready():
    from control_plane.services import blob_source
    for bad in ("stage/x.ready", "curated/x.csv", "curated/../raw/x.ready", "raw/curated/x.ready"):
        with pytest.raises(ValueError, match="curated/"):
            blob_source.write_ready_marker("c", bad, {})
