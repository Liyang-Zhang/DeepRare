from pathlib import Path

from yk_ferta.cli import main


def test_cli_manual_phenotype_mode_renders_text(tmp_path, capsys):
    config_path = tmp_path / "clinical_mvp.json"
    config_path.write_text(
        """
{
  "openai": {
    "api_key": "",
    "base_url": ""
  },
  "phenotype_extractor": {
    "enabled": false
  },
  "knowledge_searcher": {
    "enabled": false
  },
  "case_searcher": {
    "enabled": false
  }
}
        """.strip(),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--patient-id",
            "cli-001",
            "--chief-complaint",
            "Infertility",
            "--phenotype",
            "Female infertility",
            "--phenotype",
            "Oligomenorrhea",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Patient: cli-001" in output
    assert "Stage mode: manual-phenotypes" in output
    assert "Female infertility" in output
    assert "Evidence" in output
    assert "Similar Cases" in output


def test_cli_input_json_and_json_output(tmp_path, capsys):
    config_path = tmp_path / "clinical_mvp.json"
    config_path.write_text(
        """
{
  "openai": {
    "api_key": "",
    "base_url": ""
  },
  "phenotype_extractor": {
    "enabled": false
  },
  "knowledge_searcher": {
    "enabled": false
  },
  "case_searcher": {
    "enabled": false
  }
}
        """.strip(),
        encoding="utf-8",
    )
    input_path = tmp_path / "patient.json"
    input_path.write_text(
        """
{
  "patient_id": "cli-json-001",
  "chief_complaint": "Infertility for 2 years",
  "raw_note": "Irregular cycles and low AMH."
}
        """.strip(),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--input-json",
            str(input_path),
            "--output-format",
            "json",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"patient_id": "cli-json-001"' in output
    assert '"final_recommendation"' in output


def test_cli_can_save_rendered_output(tmp_path, capsys):
    config_path = tmp_path / "clinical_mvp.json"
    config_path.write_text(
        """
{
  "openai": {
    "api_key": "",
    "base_url": ""
  },
  "phenotype_extractor": {
    "enabled": false
  },
  "knowledge_searcher": {
    "enabled": false
  },
  "case_searcher": {
    "enabled": false
  }
}
        """.strip(),
        encoding="utf-8",
    )
    output_path = tmp_path / "outputs" / "result.txt"

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--patient-id",
            "cli-save-001",
            "--phenotype",
            "Female infertility",
            "--save-output",
            str(output_path),
        ]
    )

    capsys.readouterr()
    assert exit_code == 0
    assert output_path.exists()
    saved = output_path.read_text(encoding="utf-8")
    assert "Patient: cli-save-001" in saved
    assert "Evidence" in saved
