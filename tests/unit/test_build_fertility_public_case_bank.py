import csv
import json

from scripts.build_fertility_public_case_bank import build


def test_build_public_case_bank_backfills_pmc_v2_metadata(tmp_path):
    rds_path = tmp_path / "RDS.json"
    pmc_path = tmp_path / "PMC-Patients-V2.json"
    output_path = tmp_path / "fertility_public_cases_rds.csv"
    stats_path = tmp_path / "fertility_public_cases_rds.stats.json"

    rds_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "_id": "demo-1",
                        "case_report": "A case with hydatidiform mole and infertility.",
                        "diagnosis": "Hydatidiform mole",
                        "Orpha_name": "Hydatidiform mole",
                        "Orpha_id": "99927",
                        "age": [[28.0, "year"]],
                        "gender": "F",
                        "pub_date": "2024-01-01",
                    }
                ),
                json.dumps(
                    {
                        "_id": "demo-2",
                        "case_report": "A case with unrelated disease.",
                        "diagnosis": "Unrelated disease",
                        "Orpha_name": "Unrelated disease",
                        "Orpha_id": "123",
                        "age": [[20.0, "year"]],
                        "gender": "F",
                        "pub_date": "2024-01-02",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    pmc_path.write_text(
        json.dumps(
            [
                {
                    "patient_uid": "demo-1",
                    "PMID": "12345678",
                    "title": "Hydatidiform mole case report",
                    "file_path": "oa_comm/PMCxxxx/PMCdemo1.xml",
                    "pub_date": "2024-01-01",
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    build(rds_path, pmc_path, output_path, stats_path)

    with output_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["source_record_id"] == "demo-1"
    assert rows[0]["source_pmid"] == "12345678"
    assert rows[0]["source_title"] == "Hydatidiform mole case report"
    assert rows[0]["source_file_path"] == "oa_comm/PMCxxxx/PMCdemo1.xml"
    assert rows[0]["source_url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["pmc_v2_matched_source_records"] == 1
    assert stats["pmc_v2_matched_kept_records"] == 1
