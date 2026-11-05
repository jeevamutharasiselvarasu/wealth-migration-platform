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
    assert real["storageResourceId"].endswith("/providers/Microsoft.Storage/storageAccounts/afswstorage") and real["unresolved"] == ["sql", "keyVault"]
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
    assert man["simulated"] is False and man["unresolved"] == ["sql", "keyVault"] and "README.txt" in files
    assert "AccountKey" not in json.dumps(files) and not any("_Sim" in n for n in files)


def test_simulated_package_is_unchanged_and_has_no_trigger():
    pkg = _package("MIG-1", azure_simulated.simulated_environment("MIG-1"))
    assert not any(n.startswith("trigger/") for n in pkg["files"]) and json.loads(pkg["files"]["manifest.json"])["simulated"] is True


def test_real_package_reads_the_raw_files_in_place():
    env = env_mod.resolve("MIG-E439077124", CFG, "afswstorage", "xodus-lake", emulator=False)
    env["rawFolder"] = "raw/corecap"
    import pytest
    with pytest.raises(ValueError, match="No raw source file"):
        _package("MIG-E439077124", env)
    env["rawFiles"] = {"Account": "corecap_accounts.csv", "Client": "corecap_household_contacts.csv", "Household": "corecap_households.csv"}
    files = _package("MIG-E439077124", env)["files"]
    locs = {n: json.loads(t)["properties"]["typeProperties"]["location"] for n, t in files.items() if n.startswith("dataset/DS_Staging_") and "Sql" not in n}
    assert locs and all(l["container"] == "xodus-lake" and l["folderPath"] == "raw/corecap" and l["fileName"].endswith(".csv") for l in locs.values())


def test_entity_raw_file_skips_lookup_files_and_prefers_the_file_named_like_the_entity():
    got = adf_package.entity_raw_files({"Household": ["corecap_households.csv", "corecap_advisors.csv"], "Client": ["corecap_household_contacts.csv"],
                                        "Account": ["corecap_accounts.csv"]}, {"lkp_corecap_advisors"})
    assert got == {"Household": "corecap_households.csv", "Client": "corecap_household_contacts.csv", "Account": "corecap_accounts.csv"}
    assert adf_package.entity_raw_files({"Household": ["corecap_advisors.csv", "corecap_households.csv"]}, set())["Household"] == "corecap_households.csv"


def test_entity_raw_file_when_only_the_lookup_file_is_the_sample_file_but_the_master_is_also_listed():
    got = adf_package.entity_raw_files({"Household": ["corecap_advisors.csv", "corecap_households.csv", "corecap_advisors.csv"]}, {"lkp_corecap_advisors"})
    assert got["Household"] == "corecap_households.csv"
