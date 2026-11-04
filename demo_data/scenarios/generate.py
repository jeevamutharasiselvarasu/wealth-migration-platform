"""Builds the two extra CoreCap sample scenarios (deterministic; fake people).

  python demo_data/scenarios/generate.py

clean/   every household has an id, an advisor and a "Last, First" name; every account points at a household in the file and uses a
         registration type the approved crosswalk accepts (IRA, Rollover IRA); every contact has a full address. A run should succeed.
gaps_b/  the same shape with a different mix of problems from corecap_source_files: unmapped registration types, accounts that point
         at missing households, names without a comma, households without an id or advisor, contacts missing address parts.
The advisors file is copied unchanged from corecap_source_files so advisor lookups resolve.
"""
import csv
import random
import shutil
import uuid
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "corecap_source_files"
NS = uuid.UUID("5d0c7f0e-0000-4000-8000-00000000c0de")
FIRST = ["Anna", "Brian", "Carla", "Derek", "Elena", "Frank", "Grace", "Henry", "Irene", "Jacob", "Karen", "Louis", "Maria", "Nolan", "Olivia", "Peter", "Quinn", "Rosa", "Samuel", "Tina"]
LAST = ["Alder", "Brandt", "Corbin", "Dunmore", "Ellery", "Fairchild", "Garrity", "Holloway", "Iverson", "Jessup", "Kingsley", "Lockhart", "Marlowe", "Nesbitt", "Oakley", "Prescott", "Quimby", "Radcliff", "Sutter", "Thorne"]
CITIES = [("Columbus", "OH", "43215"), ("Dayton", "OH", "45402"), ("Lansing", "MI", "48933"), ("Troy", "MI", "48083"), ("Newark", "NJ", "07102"), ("Austin", "TX", "78701")]
CUSTODIANS = ["Schwab", "Schwab", "Schwab", "TD Ameritrade"]
HH_COLS = ["AdvisorId", "Id", "HouseholdName", "HouseholdProperName", "CreatedDate", "RiskScore", "RiskScoreDate", "RiskToleranceScore", "RiskToleranceScoreDate", "DeletedDate", "DeletedByUserName"]
ACC_COLS = ["AccountId", "AccountNumber", "AccountName", "Balance", "CustodianName", "TaxStatus", "RegistrationType", "DefaultLiquidationMethod", "RepCode", "HouseholdId", "LastDirectReconDate", "ReportedAsOfDate", "CustodialStartDate", "ClosedDate", "RiskScore", "RiskScoreDate"]
CON_COLS = ["HouseholdId", "ContactId", "Salutation", "FirstName", "Nickname", "MiddleName", "LastName", "Suffix", "Designations", "JobTitle", "EmployerName", "EntityType", "DateOfBirth", "TaxId", "HouseholdOrder", "HouseholdRelationshipType", "Address1", "Address2", "City", "State", "Zip", "Country", "Phone", "Email"]


def uid(tag, n):
    return str(uuid.uuid5(NS, f"{tag}-{n}")).upper()


def build(tag, households, gaps):
    rnd = random.Random(tag)
    advisors = [r["Id"] for r in csv.DictReader(open(SRC / "corecap_advisors.csv", newline=""))]
    hh, con, acc = [], [], []
    for i in range(households):
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7 + 3) % len(LAST)]
        hid, adv = uid(tag + "-hh", i), advisors[i % len(advisors)]
        name = f"{last}, {first}"
        created = (date(2022, 1, 1) + timedelta(days=rnd.randint(0, 1400))).isoformat() + " 10:00:00"
        hh.append({"AdvisorId": adv, "Id": hid, "HouseholdName": name, "HouseholdProperName": f"{first} {last}", "CreatedDate": created, "RiskScore": rnd.randint(10, 90),
                   "RiskScoreDate": "2026-08-03 22:00:00", "RiskToleranceScore": round(rnd.uniform(20, 90), 4), "RiskToleranceScoreDate": "2026-01-10 00:00:00", "DeletedDate": "", "DeletedByUserName": ""})
        city, st, zp = CITIES[i % len(CITIES)]
        for order, rel, fn in ((1, "Head", first), (2, "Spouse", FIRST[(i + 5) % len(FIRST)])):
            if order == 2 and i % 3:
                continue
            con.append({"HouseholdId": hid, "ContactId": uid(tag + "-ct", f"{i}-{order}"), "Salutation": "", "FirstName": fn, "Nickname": "", "MiddleName": "", "LastName": last, "Suffix": "",
                        "Designations": "", "JobTitle": "", "EmployerName": "", "EntityType": "individual", "DateOfBirth": "[REDACTED]", "TaxId": "[REDACTED]", "HouseholdOrder": order,
                        "HouseholdRelationshipType": rel, "Address1": f"{100 + i * 13} Maple Ave", "Address2": "", "City": city, "State": st, "Zip": zp, "Country": "US",
                        "Phone": f"(555) 01{i:02d}-{1000 + i}", "Email": "[REDACTED]"})
        for k in range(1 + i % 3):
            reg = ("IRA", "Rollover IRA")[(i + k) % 2]
            acc.append({"AccountId": uid(tag + "-ac", f"{i}-{k}"), "AccountNumber": 20000000 + i * 10 + k, "AccountName": f"{last.upper()}, {first.upper()} - {reg}",
                        "Balance": round(rnd.uniform(5000, 400000), 2), "CustodianName": CUSTODIANS[(i + k) % 4], "TaxStatus": "TaxDeferred", "RegistrationType": reg,
                        "DefaultLiquidationMethod": "FIFO", "RepCode": str(8000000 + (i % len(advisors)) * 1111), "HouseholdId": hid,
                        "LastDirectReconDate": "2026-08-03 00:00:00", "ReportedAsOfDate": "2026-08-03 00:00:00",
                        "CustodialStartDate": (date(2015, 1, 1) + timedelta(days=rnd.randint(0, 3000))).isoformat() + " 00:00:00", "ClosedDate": "",
                        "RiskScore": rnd.randint(10, 90), "RiskScoreDate": "2026-08-02 22:09:00"})
    if gaps:
        for j in (2, 9, 14, 21, 26, 33, 38, 44):           # registration types the crosswalk rejects
            acc[j]["RegistrationType"] = ("Roth IRA", "Trust", "Inherited IRA", "Individual", "SEP IRA", "", "Estate", "Roth IRA")[(j // 5) % 8]
        for j in (4, 17, 30):                                # accounts pointing at households that are not in the file
            acc[j]["HouseholdId"] = uid(tag + "-missing", j)
        acc[11]["AccountNumber"] = ""
        acc[12]["CustodianName"] = ""
        acc[40]["AccountId"] = acc[41]["AccountId"]          # duplicate account id
        for j in (3, 11):                                    # corporate-style names with no comma
            hh[j]["HouseholdName"] = f"{LAST[j]} Family Trust"
        hh[6]["Id"] = ""                                     # no household id
        for j in (8, 15, 22):                                # advisor missing from the advisors file
            hh[j]["AdvisorId"] = uid(tag + "-noadv", j)
        hh[19]["AdvisorId"] = ""
        con[1]["Zip"] = ""; con[5]["Zip"] = ""
        con[7]["State"] = ""
        con[9]["Address1"] = ""
        con[12]["City"] = ""
        con[14]["HouseholdId"] = uid(tag + "-ghost", 1)      # contact for an unknown household
    return hh, con, acc


def write(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\r\n")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    for tag, n, gaps in (("clean", 30, False), ("gaps_b", 45, True)):
        out = HERE / tag
        out.mkdir(exist_ok=True)
        hh, con, acc = build(tag, n, gaps)
        write(out / "corecap_households.csv", HH_COLS, hh)
        write(out / "corecap_household_contacts.csv", CON_COLS, con)
        write(out / "corecap_accounts.csv", ACC_COLS, acc)
        shutil.copyfile(SRC / "corecap_advisors.csv", out / "corecap_advisors.csv")
        print(tag, len(hh), "households", len(con), "contacts", len(acc), "accounts")
