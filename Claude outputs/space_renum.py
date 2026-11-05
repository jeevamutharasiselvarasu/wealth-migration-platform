import re,sys,os
root=os.path.expanduser('~/mnt/wealth-migration-platform/ui/space_pages')
files=['data-migration.html','deal-room.html','governance.html','tenant.html']
rep_log=[]
def rep(s,old,new,fname,tag,expect=1):
    c=s.count(old)
    rep_log.append((fname,tag,c,expect))
    return s.replace(old,new)

COMMON=[
("gates",
"""G('M5','dm','05 Execution',['MA','VS'],'Run IDs and load logs',{zone:'Transformed',dual:true}),
 G('M6','dm','06 Reconciliation',['VS'],'Tolerances and break report',{zone:'Transformed'}),""",
"""G('M5','dm','05 Mapping Validation Review',['MA'],'Canonical coverage and exception list',{zone:'Transformed'}),
 G('M6','dm','06 Transformation SQL',['MA'],'Transformation SQL and target mapping version',{zone:'Transformed'}),
 G('M7','dm','07 ADF Execution',['MA','VS'],'Run IDs and load logs',{zone:'Transformed',dual:true}),
 G('M8','dm','08 Target validation',['VS'],'Target checks, custom cases and break report',{zone:'Transformed'}),"""),
("isDP","function isDP(g){return g.id=='M5'||g.id=='M6'}","function isDP(g){return g.id=='M7'||g.id=='M8'}"),
("cmt1","Control plane: stages 01 to 04 set the migration up","Control plane: stages 01 to 06 set the migration up"),
("cmt2","Data plane: stages 05 and 06 are runs","Data plane: stages 07 and 08 are runs"),
("label1","if(g.id=='M4'&&g.st=='approved')return 'Bundle published'","if(g.id=='M6'&&g.st=='approved')return 'Bundle published'"),
("label2","var p=ph(g);if(g.id=='M5')return ({none:'Waits for the bundle'","var p=ph(g);if(g.id=='M7')return ({none:'Waits for the bundle'"),
("scn_v","ST={v:3,scn:n","ST={v:4,scn:n"),
("scn_ap","n=='run'?['D1','D2','D3','D4','M1','M2','M3','M4']","n=='run'?['D1','D2','D3','D4','M1','M2','M3','M4','M5','M6']"),
("scn_m5","var m5=gate('M5');m5.st='review';m5.ph='results';","var m7=gate('M7');m7.st='review';m7.ph='results';"),
("scn_log","'01 to 04 approved, artifact bundle v1 published'","'01 to 06 approved, artifact bundle v1 published'"),
("load","ST.v===3&&ST.gates&&ST.gates.length==12","ST.v===4&&ST.gates&&ST.gates.length==14"),
("appr_dp1","if(g.id=='M5'){var m6=gate('M6');m6.st='review';m6.ph='results'}","if(g.id=='M7'){var m8=gate('M8');m8.st='review';m8.ph='results'}"),
("appr_dp2","else if(g.id=='M6'){var lr=ST.runs","else if(g.id=='M8'){var lr=ST.runs"),
("appr_pub","if(g.id=='M4'){ST.bundle.pub=true;logit('dm','04 Schema Mapper approved. Artifact bundle v'+ST.bundle.v+' published to the data plane',ROLES[role]);var m5=gate('M5');m5.st='running';m5.ph='ready';",
 "if(g.id=='M6'){ST.bundle.pub=true;logit('dm','06 Transformation SQL approved. Artifact bundle v'+ST.bundle.v+' published to the data plane',ROLES[role]);var m7=gate('M7');m7.st='running';m7.ph='ready';"),
("finish","if(g.id=='M5'){if(ph(g)!='run')return;","if(g.id=='M7'){if(ph(g)!='run')return;"),
("chg","var m5=gate('M5'),m6=gate('M6');m5.st='running';m5.ph='ready';m5.by=[];m6.st='queued';m6.ph='none';m6.by=[];logit('dm',(lr?lr.id:'Run')+' rejected",
 "var m7=gate('M7'),m8=gate('M8');m7.st='running';m7.ph='ready';m7.by=[];m8.st='queued';m8.ph='none';m8.by=[];logit('dm',(lr?lr.id:'Run')+' rejected"),
("sr1","function startRun(role){var m5=gate('M5'),m6=gate('M6');","function startRun(role){var m7=gate('M7'),m8=gate('M8');"),
("sr2","if(m5.st=='running'&&m5.ph=='run')return{ok:false,msg:'A run is already in progress.'};","if(m7.st=='running'&&m7.ph=='run')return{ok:false,msg:'A run is already in progress.'};"),
("sr3","m5.st='running';m5.ph='run';m5.by=[];m6.st='queued';m6.ph='none';m6.by=[];","m7.st='running';m7.ph='run';m7.by=[];m8.st='queued';m8.ph='none';m8.by=[];"),
("rb1","function reviseBundle(role){var m4=gate('M4'),m5=gate('M5'),m6=gate('M6');","function reviseBundle(role){var m6=gate('M6'),m7=gate('M7'),m8=gate('M8');"),
("rb2","if(m5.st=='running'&&m5.ph=='run')return{ok:false,msg:'Wait for the current run to finish.'};","if(m7.st=='running'&&m7.ph=='run')return{ok:false,msg:'Wait for the current run to finish.'};"),
("rb3","m4.st='running';m4.by=[];[m5,m6].forEach(","m6.st='running';m6.by=[];[m7,m8].forEach("),
("chk","if(ST.bundle.pub!=(gate('M4').st=='approved'))bad.push('M4');if((ST.runs.length||['run','results','done'].indexOf(ph(gate('M5')))>-1)&&!ST.bundle.pub)bad.push('M5');",
 "if(ST.bundle.pub!=(gate('M6').st=='approved'))bad.push('M6');if((ST.runs.length||['run','results','done'].indexOf(ph(gate('M7')))>-1)&&!ST.bundle.pub)bad.push('M7');"),
("chk_d","published only when the Schema Mapper gate is approved","published only when the Transformation SQL gate is approved"),
("role1","owns gates 01 to 04. Co-signs the load.","owns gates 01 to 06. Co-signs the ADF execution."),
("role2","Owns reconciliation and co-signs the load.","Owns target validation and co-signs the ADF execution."),
]

DM=[
("stages",
"{id:'M5',n:'05',name:'Execution',zone:'transformed',owner:'Validation Specialist',gate:true,enabled:true},{id:'M6',n:'06',name:'Reconciliation',zone:'transformed',owner:'Validation Specialist',gate:true,enabled:true}",
"{id:'M5',n:'05',name:'Mapping Validation Review',zone:'transformed',owner:'Migration Analyst',gate:true,enabled:true},{id:'M6',n:'06',name:'Transformation SQL',zone:'transformed',owner:'Migration Analyst',gate:true,enabled:true},{id:'M7',n:'07',name:'ADF Execution',zone:'transformed',owner:'Validation Specialist',gate:true,enabled:true},{id:'M8',n:'08',name:'Target validation',zone:'transformed',owner:'Validation Specialist',gate:true,enabled:true}"),
("members1","scope:'Stages 01 to 04'","scope:'Stages 01 to 06'"),
("members2","scope:'Stages 05 and 06'","scope:'Stages 07 and 08'"),
("strip","grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:12px","grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px"),
("stcM5"," M5:{sum:'Loads the Transformed zone into the target through Data Factory once stage 04 is approved.',kp:[['Batches','Not run'],['Loaded','—'],['Failed','—'],['Target','Orion APIs']]",
""" M5:{sum:'Checks the source-to-canonical mapping before any SQL is written: every canonical field mapped, every exception listed.',kp:[['Canonical fields mapped','142 of 158'],['Required fields unmapped','4'],['Exceptions','14'],['Reviewed','74%']],cols:['Target entity','Fields mapped','Exceptions','Status'],num:[1,2],rows:[['Households','24 of 24','0','Complete'],['Accounts','38 of 40','2','Review'],['Positions','31 of 31','0','Complete'],['Transactions','27 of 27','0','Complete'],['Cost basis lots','22 of 26','4','Review'],['Performance series','0 of 10','10','Review']],exc:'Approve this gate only when every required canonical field is mapped or its gap is accepted.'},
 M6:{sum:'The canonical-to-target mapping, written as SQL. The data plane reads the source files from Azure Blob storage, runs this SQL and loads the target SQL database.',kp:[['SQL statements','6'],['Target tables','6'],['Rules compiled','9'],['Status','Draft']],cols:['Target table','Built from','Rules','Status'],num:[2],rows:[['Households','canonical.households','2','Draft'],['Accounts','canonical.accounts','3','Draft'],['Positions','canonical.positions','2','Draft'],['Transactions','canonical.transactions','1','Draft'],['Cost basis lots','canonical.cost_basis_lots','1','Draft'],['Performance series','canonical.performance','0','Draft']],exc:''},
 M7:{sum:'Reads the source files from Azure Blob storage, runs the approved transformation SQL through Data Factory and loads the target SQL database once stage 06 is approved.',kp:[['Batches','Not run'],['Loaded','—'],['Failed','—'],['Target','Target SQL database']]"""),
("stcM6"," M6:{sum:'Independently reconciles the migrated book back to source once the load completes.'",
 " M8:{sum:'Runs the built-in checks (schema, data types, nullability) on the populated target, then the custom cases from the Validation Specialist, and reconciles back to source.'"),
("cps","var CPS=['M1','M2','M3','M4'];","var CPS=['M1','M2','M3','M4','M5','M6'];"),
("src","M4:'04 Schema Mapper',M5:'Target settings',M6:'Validation rules'}","M4:'04 Schema Mapper',M5:'05 Mapping Validation Review',M6:'06 Transformation SQL',M7:'07 ADF Execution',M8:'08 Target validation'}"),
("bl","['M1','M2','M3','M4','M5','M6'].forEach(function(id){STG[id].arts","['M1','M2','M3','M4','M5','M6','M7','M8'].forEach(function(id){STG[id].arts"),
("art","if(id=='M5'||id=='M6'){return XS.gate('M4').st=='review'","if(id=='M7'||id=='M8'){return XS.gate('M6').st=='review'"),
("zones","['Control plane · setup',['M1','M2','M3','M4'],'landing'],['Data plane · runs',['M5','M6'],'staged']","['Control plane · setup',['M1','M2','M3','M4','M5','M6'],'landing'],['Data plane · runs',['M7','M8'],'staged']"),
("cp1","msg=id=='M4'?'<b>Gate cleared.</b> Artifact bundle v'","msg=id=='M6'?'<b>Gate cleared.</b> Artifact bundle v'"),
("cp2","(id=='M4'?'Approving publishes artifact bundle v'","(id=='M6'?'Approving publishes artifact bundle v'"),
("cp3","(id=='M4'?'Approve and publish bundle v'+B.v:'Approve gate')","(id=='M6'?'Approve and publish bundle v'+B.v:'Approve gate')"),
("cp4","else if(id=='M4'&&g.st=='approved')acts='<button class=\"btn p\" data-s=\"M5\" type=\"button\">Open the data plane</button>'","else if(id=='M6'&&g.st=='approved')acts='<button class=\"btn p\" data-s=\"M7\" type=\"button\">Open the data plane</button>'"),
("cp5","var arts=id=='M4'?'<div class=\"card\"><h3>Artifact bundle v'","var arts=id=='M6'?'<div class=\"card\"><h3>Artifact bundle v'"),
("cp6","that stage 04 publishes to the data plane","that stage 06 publishes to the data plane"),
("cp7","/* ---- control plane stages 01 to 04 ---- */","/* ---- control plane stages 01 to 06 ---- */"),
("ex1","function vExec(){var id='M5',","function vExec(){var id='M7',"),
("ex2","Approve stage 04 Schema Mapper to publish bundle v","Approve stage 06 Transformation SQL to publish bundle v"),
("ex3","<button class=\"btn g sm\" data-s=\"M4\" type=\"button\">Go to the control plane</button>","<button class=\"btn g sm\" data-s=\"M6\" type=\"button\">Go to the control plane</button>"),
("ex4","Open 06 Reconciliation for the detail.","Open 08 Target validation for the detail."),
("ex5","'Runs the published bundle on the real data, from ingestion to transformation and the load into the target. Each run triggers the validation agent.'","'Runs the published bundle on the real data: reads the source files from Azure Blob storage, runs the transformation SQL and loads the target SQL database. Each run triggers the validation agent.'"),
("ex6","/* ---- data plane stage 05: runs ---- */","/* ---- data plane stage 07: ADF execution ---- */"),
("rc1","function vRecon(){var id='M6',","function vRecon(){var id='M8',"),
("rc2","Waits for the run to be approved at 05 Execution.","Waits for the run to be approved at 07 ADF Execution."),
("rc3","'The validation agent runs after every run, using the validation rules and the reconciliation measures. The Validation Specialist decides.'","'Target validation runs after every ADF run on the populated target: the built-in checks (schema, data types, nullability), the custom rules from the Validation Specialist and the reconciliation measures. The Validation Specialist decides.'"),
("rc4","/* ---- data plane stage 06: validation and reconciliation ---- */","/* ---- data plane stage 08: target validation ---- */"),
("vs","id=='M5'?vExec():vRecon()","id=='M7'?vExec():vRecon()"),
("rule1","Active rules run in stage 06 Reconciliation.</p>","Active rules run in stage 08 Target validation.</p>"),
("rule2","Active rules run in stage 06 Reconciliation.',canV","Active rules run in stage 08 Target validation.',canV"),
]

NEW_M4_TO_M7=r""" M4:{def:'The mapping from source fields to the canonical model.',gen:'Source-to-canonical mapping SQL, one view per canonical entity.',run:'Reshape the Staged records into the canonical model as a dry run.',
  tel:{run:'RUN-0144',at:'10:40',dur:'6m 18s',rin:1760494,rout:1760480,rej:14,acts:'16 of 16'},
  arts:[{n:'map_to_canonical.sql',t:'SQL',d:'One view per canonical entity, built from the approved source mapping.',code:`CREATE OR ALTER VIEW canonical.accounts AS\nSELECT a.account_id AS source_account_id,\n       a.account_type,\n       h.household_id\nFROM staged.accounts a\nJOIN staged.households h ON h.source_key = a.household_key;`}],
  ev:[['10:40','info','Mapping dry run started'],['10:46','warn','14 positions have no security identifier']]},
 M5:{def:'The checks on the source-to-canonical mapping.',gen:'Coverage queries that list canonical fields with no source and mappings with an exception.',run:'Check the mapping against the Staged records before any transformation SQL is written.',
  tel:{run:'RUN-0144',at:'10:50',dur:'1m 40s',rin:1760494,rout:1760480,rej:14,acts:'4 of 4'},
  arts:[{n:'mapping_coverage.sql',t:'SQL',d:'Lists canonical fields with no mapped source column and required fields left unmapped.',code:`SELECT f.entity, f.field_name, f.is_required\nFROM canonical.field_catalog f\nLEFT JOIN mapping.approved m ON m.canonical_field = f.field_name\nWHERE m.canonical_field IS NULL;`}],
  ev:[['10:50','info','Coverage check: 142 of 158 fields mapped'],['10:51','warn','14 positions have no security identifier']]},
 M6:{def:'The canonical-to-target mapping, written as SQL.',gen:'Transformation SQL that reads the canonical model and fills the target tables, with each approved rule compiled in.',run:'Run the SQL on sample data as a dry run.',
  tel:{run:'RUN-0144',at:'11:00',dur:'2m 10s',rin:1760480,rout:1760480,rej:0,acts:'6 of 6'},
  arts:[{n:'map_to_target_contract.sql',t:'SQL',d:'One statement per target table, built from the approved canonical-to-target mapping.',code:`INSERT INTO target.accounts (account_id, account_type, household_id)\nSELECT a.source_account_id, m.target_account_type, a.household_id\nFROM canonical.accounts a\nJOIN ref.account_type_map m ON m.source_type = a.account_type;`},
   {n:'transformation_rules.sql',t:'SQL',d:'The approved transformation rules, written as SQL expressions and applied by the statements above.',code:`-- Registration type crosswalk\nCASE UPPER(TRIM(src.reg_cd))\n  WHEN 'IND' THEN 'Individual'\n  WHEN 'JNT' THEN 'Joint'\nEND AS registration_type`}],
  ev:[['11:00','info','Transformation SQL generated for 6 target tables'],['11:02','info','Dry run on sample data passed']]},
 M7:{def:'The load order and the target database settings.',gen:'The Data Factory pipeline that reads the source files from Azure Blob storage, runs the transformation SQL and loads the target SQL database.',run:'Read the source files, transform them with the approved SQL and load the target in batches.',
  tel:{run:'RUN-0145',at:'11:20',dur:'18m 47s',rin:1760480,rout:1760480,rej:0,acts:'12 of 12'},
  arts:[{n:'pl_transform_and_load.arm.json',t:'ADF ARM template',d:'Copies the source files from Azure Blob storage, runs the approved transformation SQL and loads the target SQL database, in entity order.',code:`{ "name": "pl_transform_and_load", "properties": { "activities": [\n  { "name": "CopyBlobToStaging", "type": "Copy" },\n  { "name": "RunTransformationSql", "type": "Script",\n    "dependsOn": [{ "activity": "CopyBlobToStaging", "dependencyConditions": ["Succeeded"] }],\n    "typeProperties": { "scripts": [{ "text": "EXEC target.usp_load_all" }] } }\n] } }`},
   {n:'load_preflight.sql',t:'SQL',d:'Checks that every parent record exists before its children are loaded.',code:`SELECT COUNT(*) AS orphans\nFROM target.accounts a\nLEFT JOIN target.households h ON h.household_id = a.household_id\nWHERE h.household_id IS NULL;`}],
  ev:[['11:20','info','Load started for 4 entities'],['11:39','info','Batch 12 of 12 loaded, 0 failed']]},
"""
BUILTIN=r"""arts:[{n:'target_builtin_checks.sql',t:'SQL',d:'Built-in checks on the populated target: schema, data types and nullability.',code:`SELECT t.table_name, t.column_name\nFROM contract.target_columns t\nJOIN information_schema.columns c\n  ON c.table_name = t.table_name AND c.column_name = t.column_name\nWHERE t.is_required = 1 AND c.is_nullable = 'YES';`},
   {n:'reconcile_book.sql'"""
M8_OLD_HEAD=" M6:{def:'The measures and tolerances to reconcile.',gen:'Reconciliation SQL and the validation rules, compiled together.',run:'Compare the migrated book with the source, independently.',"
M8_NEW_HEAD=" M8:{def:'The built-in target checks, the custom cases and the measures to reconcile.',gen:'Target validation SQL: schema, data type and nullability checks, the validation rules and the reconciliation queries.',run:'Check the populated target, then compare the migrated book with the source, independently.',"

for f in files:
    p=os.path.join(root,f); s=open(p,encoding='utf-8').read()
    for tag,old,new in COMMON: s=rep(s,old,new,f,tag)
    if f=='governance.html':
        s=rep(s,"grp('dm','Data Migration',['M1','M2','M3','M4','M5','M6'])","grp('dm','Data Migration',['M1','M2','M3','M4','M5','M6','M7','M8'])",f,'grp')
    if f=='data-migration.html':
        for tag,old,new in DM: s=rep(s,old,new,f,tag)
        a=s.index(" M4:{def:'The mapping from source fields to the target contract.'")
        c=s.index(M8_OLD_HEAD)
        e=s.index("/* ===== control plane and data plane views ===== */")
        old_m6=s[c:e]
        assert old_m6.count("arts:[{n:'reconcile_book.sql'")==1
        new_m8=old_m6.replace(M8_OLD_HEAD,M8_NEW_HEAD,1).replace("arts:[{n:'reconcile_book.sql'",BUILTIN,1)
        s=s[:a]+NEW_M4_TO_M7+new_m8+s[e:]
        rep_log.append((f,'stg',1,1))
    open(p,'w',encoding='utf-8').write(s)
bad=[r for r in rep_log if r[2]!=r[3]]
print('total edits',len(rep_log),'mismatches',len(bad))
for r in bad: print(r)
