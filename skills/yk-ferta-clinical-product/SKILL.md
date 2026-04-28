---
name: yk-ferta-clinical-product
description: Use when designing, reviewing, or implementing yk-FERTA, infertility/fertility clinical assistant workflows, DeepRare-style clinical reasoning products, traceable medical AI outputs, phenotype-driven diagnosis support, evidence review, or local/public case-bank retrieval for clinical use.
metadata:
  short-description: Clinical product design principles for yk-FERTA-style fertility AI
---

# yk-FERTA Clinical Product Design

Use this skill when working on yk-FERTA or similar clinical AI products for infertility, fertility genetics, phenotype-driven diagnosis support, or DeepRare-style traceable reasoning.

## Core Philosophy

Build the product as a **traceable clinical reasoning system**, not as a single prompt that produces a final answer.

The system should decompose diagnosis support into auditable stages:

1. Clinical input normalization
2. Phenotype / HPO extraction
3. Human confirmation of phenotype terms
4. Phenotype-based candidate generation
5. Online and local evidence retrieval
6. Similar case retrieval
7. First-round diagnosis synthesis
8. Per-candidate evidence verification
9. Reflection over support, contradiction, and missing evidence
10. Final physician-facing summary

Each stage should produce artifacts that a clinician or developer can inspect.

## Product Principles

- **Human-in-the-loop phenotype confirmation is mandatory.** Clinical text can be sparse, noisy, or misleading. Let users remove wrong HPO terms and add missing ones before diagnosis reasoning.
- **Use rules for structure, not clinical judgment.** Rules should validate inputs, label evidence roles, control retrieval scope, and filter obvious noise. They should not hard-code disease-specific conclusions unless the rule is a formal, stable domain constraint.
- **Let LLMs handle clinical synthesis and review.** Candidate ranking, evidence weighing, contradiction analysis, and missing-information reasoning should happen in LLM synthesis/review stages with explicit evidence boundaries.
- **Do not overfit demo cases.** A葡萄胎 case, POI case, male-factor case, or DSD case should not lead to one-off keyword rules that reduce generalizability.
- **Prefer evidence layering over one perfect source.** Combine phenotype tools, literature, guidelines, public cases, private historical testing cases, and disease mappings, but keep each source's role explicit.
- **Make outputs auditable.** Show which HPO terms, tools, literature, similar cases, and review decisions contributed to each final conclusion.

## Evidence Role Boundaries

Always distinguish evidence types:

- `diagnosis_reference`: public case or curated case with a known diagnosis. Can support diagnostic similarity.
- `testing_finding_reference`: private historical testing case without final physician diagnosis. Can support phenotype-gene relevance or testing strategy, but cannot confirm the current patient's diagnosis.
- `knowledge_reference`: guideline, literature, database, or web evidence. Supports disease knowledge, diagnostic criteria, management, or differential reasoning.
- `phenotype_tool_hint`: PubCaseFinder, Phenobrain, HPO association, or other phenotype-based candidate suggestion. Treat as candidate-generation evidence, not final diagnosis.

Private historical testing data usually lacks confirmed final diagnosis. Never treat it as equivalent to a diagnosed case.

## Molecular Evidence Policy

Clinical diagnosis and molecular confirmation must be separated.

If the current patient has no patient-specific genetic or variant result:

- Do not say the disease is “caused by NLRP7 mutations”, “due to KHDC3L mutations”, or equivalent.
- Use clinical wording such as “suspected recurrent hydatidiform mole” or “clinical presentation compatible with X”.
- Mention genes only as disease-associated molecular etiologies or recommended testing targets.
- Explicitly state that molecular confirmation is missing when reviewing candidate diseases.

If patient-specific molecular results are available:

- State which variant/gene result supports the candidate.
- Distinguish pathogenic/likely pathogenic/VUS/CNV/negative findings.
- Do not use historical private cases as a substitute for current patient molecular evidence.

Good output pattern:

- Clinical diagnosis: suspected familial recurrent hydatidiform mole
- Disease molecular information: NLRP7/KHDC3L are known associated genes, pending patient-specific testing
- Recommended confirmation: targeted sequencing / WES / methylation or imprinting analysis as appropriate

Bad output pattern:

- Recurrent hydatidiform mole due to NLRP7 mutations

## Workflow Design Guidance

### HPO Stage

- Extract HPO automatically, but assume extraction can be wrong.
- Show extracted HPO label, code, source, confidence, and notes.
- Let users remove false positives and add HPO terms from CHPO/HPO catalogs.
- Diagnosis should not proceed until HPO confirmation is complete.

### Phenotype Analyser

Use phenotype tools as candidate generators:

- PubCaseFinder, if available and stable
- Phenobrain
- HPO disease association pages or local HPO-disease mappings

Outputs should be labeled as hints, not diagnoses.

### Knowledge Searcher

Online search is supporting evidence, not the main knowledge base.

Prioritize:

- Guidelines and consensus documents when available
- PubMed / GeneReviews / OMIM / Orphanet / authoritative medical databases
- Domain-specific curated knowledge cards

Avoid letting generic web snippets dominate reasoning.

### Case Searcher

Retrieve both public and private cases, but label them differently.

Public cases can suggest likely diagnoses when the case diagnosis is known.

Private testing cases should answer:

- Have we seen similar phenotype combinations?
- Which genes/variant types were reported as phenotype-relevant?
- What tests were useful?

They should not answer:

- What disease does the current patient definitely have?

### Candidate Synthesis

First-round diagnosis should produce a ranked differential, not a final answer.

For each candidate, include:

- Why it fits
- Which phenotypes support it
- Which retrieved sources suggested it
- Whether it requires genetic, biochemical, imaging, endocrine, or reproductive-history confirmation

### Candidate Verification

Review each candidate independently.

Ask:

- What supports this candidate?
- What contradicts it?
- What key phenotype or lab data is missing?
- Is this a clinical syndrome, disease-level molecular knowledge, or a patient-specific gene-confirmed diagnosis?
- Are private cases only testing references?

Do not let a high phenotype match automatically become a confirmed diagnosis.

### Final Output

Final output should be clinician-facing and traceable:

- Clinical diagnosis / differential diagnosis
- Confidence or support level
- Disease-associated genes / molecular mechanism, if relevant
- Supporting evidence
- Contradicting evidence
- Missing evidence
- Recommended next tests
- Management or referral suggestions, when appropriate
- Cautions and uncertainty

The final answer should not hide uncertainty.

## Engineering Guidance

- Keep stage artifacts stable and serializable.
- Prefer structured outputs over free-form-only summaries.
- Store source IDs, URLs, role labels, confidence scores, and timestamps when possible.
- UI should show both final answer and intermediate reasoning artifacts.
- Add tests for evidence-role boundaries and molecular-confirmation wording.
- Avoid leaking API keys or private patient identifiers into logs, docs, or committed files.

## Review Checklist

Before accepting a change, check:

- Does it preserve mandatory HPO confirmation?
- Does it separate clinical diagnosis from molecular confirmation?
- Does it label public cases and private testing cases differently?
- Does it avoid hard-coded conclusions for one disease or one demo case?
- Does the UI expose enough artifacts for audit?
- Does the final output make uncertainty and missing evidence visible?
- Would a clinician understand why each candidate was included or rejected?
