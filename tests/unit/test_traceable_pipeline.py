from yk_ferta.agents.factory import build_default_pipeline
from yk_ferta.schemas.pipeline import PipelineRequest
from yk_ferta.schemas.clinical import PatientProfile


def test_default_pipeline_returns_structured_response():
    pipeline = build_default_pipeline()
    request = PipelineRequest(
        patient=PatientProfile(
            patient_id="case-001",
            chief_complaint="Infertility for 3 years",
            history="Irregular menstrual cycles with suspected endocrine issues.",
            laboratory_findings="AMH below expected range.",
        ),
        top_k=3,
    )

    response = pipeline.run(request)

    assert response.patient_id == "case-001"
    assert len(response.evidence) == 1
    assert len(response.candidates) == 1
    assert response.recommendation.cautions
    assert "not suitable for clinical use" in response.recommendation.cautions[0].lower()


def test_default_pipeline_uses_requested_top_k_ceiling():
    pipeline = build_default_pipeline()
    request = PipelineRequest(
        patient=PatientProfile(patient_id="case-002", raw_note="Minimal case note"),
        top_k=1,
    )

    response = pipeline.run(request)

    assert len(response.candidates) <= 1
