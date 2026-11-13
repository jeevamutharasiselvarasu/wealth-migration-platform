# Creating the Simplicity live database

The live database is where XODUS promotes validated batches. XODUS only reads its catalog and counts; Data Factory (managed identity) writes to it.
The scripts are in `deploy/simplicity/` and only create what is missing; they never drop or alter.

## Steps (an Entra admin of the Azure SQL server does 1 to 4)

1. **Create the database.** Connect to the server's `master` database and run `00_create_database.sql` (creates `Simplicity`; change the tier in the file if needed).
2. **Create the tables.** Connect to the `Simplicity` database itself (Azure SQL has no `USE`) and run:
   - `01_live_tables.sql`: the earlier single-schema layout, schema `simplicity`, tables `ORION_MCL_HOUSEHOLD`, `ORION_MCL_CLIENT`, `ORION_MCL_ACCOUNT`.
   - `03_orion_sparc_tables.sql`: the layout of the CoreCap to Simplicity spec: schema `orion` (3 tables, 27 columns) and schema `sparc` (5 tables, 34 columns).
   Run only the one your migration targets (see "Which schema" below).
3. **Grant access.** In the `Simplicity` database run `02_access.sql` after replacing the two names in angle brackets (the platform SQL login: read only; the Data Factory name: read, write and the `xodus_landing` schema). The database-wide roles already cover new schemas.
4. **Link keys.** `03_orion_sparc_tables.sql` adds nullable link columns (`HH_GROUPING_CODE`, account number) as a proposal for decision D4. Change or drop them when Simplicity decides; XODUS reads whatever the live tables hold.
5. **Platform Setup (a Platform Admin).** Live database settings: server, database `Simplicity`, the SQL login, the environment variable that holds its password, and the schema name. Test the connection, then infer and confirm the target model (Target setup).
6. **Factory linked services.** `LS_SQL_Simplicity` points at the `Simplicity` database, `LS_SQL_Target` at the XODUS database, `LS_BLOB` at the lake.

## Which schema: one platform connection, one schema per migration

The connection (server, database `Simplicity`, SQL login) is set once in Platform Setup. The schema is chosen per migration, so Orion MCL and SPARC can be two migrations
on the same database:

1. **Platform Admin, once per schema.** Target setup > "Schema being set up": pick `orion`, then configure, infer, review and confirm its target model. Repeat for `sparc`.
   Each schema keeps its own inferred and confirmed model; reopening one leaves the other alone. (The list comes from the live catalog, with system and `xodus_*` schemas left out.)
2. **Migration setup.** When more than one schema is confirmed, the first card asks "Which schema does this migration load into?". The entities offered are the ones that
   schema's model receives (Orion: Account, Client, Household; SPARC: its tables). The choice is saved with the migration and used for its target mapping, package (`liveSchema`),
   promotion and deployment check.
3. A target model saved before this change is adopted as the model of the schema its endpoint names.

Table names may repeat across schemas (`household` in `orion` and in `sparc`): each migration only ever sees the tables of its own schema.

## Raw files

The batch reads the source files from the lake: container `xodus-lake`, folder `raw/<source firm>` (for the Amplify migrations `raw/amplify`; the sample files for discovery go in `sample-raw/amplify`). Upload the CSVs there before starting a batch; the setup pipeline and batch overwrite the XODUS tables, never the live ones.
