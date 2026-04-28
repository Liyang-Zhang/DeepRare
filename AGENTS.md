# AGENTS.md

This repository contains the DeepRare-derived yk-FERTA clinical MVP work. When working in this repo, prioritize yk-FERTA's product direction over the original DeepRare research-code structure.

## Local Skills

### yk-ferta-clinical-product

Use the local skill at:

`skills/yk-ferta-clinical-product/SKILL.md`

Use this skill whenever the task involves:

- yk-FERTA clinical product design
- infertility / fertility clinical assistant workflows
- HPO / phenotype extraction, confirmation, or review
- DeepRare-style traceable diagnosis reasoning
- public/private case-bank retrieval
- evidence role boundaries
- molecular confirmation wording
- clinical UI artifacts for audit and traceability

The skill encodes the main product philosophy:

- Build a staged, traceable reasoning system, not a one-shot prompt.
- Keep HPO human confirmation mandatory.
- Use rules for structure and evidence labeling, not disease-specific clinical judgment.
- Let LLM stages handle synthesis, candidate review, contradiction analysis, and reflection.
- Separate clinical diagnosis from molecular confirmation.
- Treat private historical testing cases as `testing_finding_reference`, not confirmed diagnosis evidence.
- Make final outputs auditable, with supporting evidence, contradicting evidence, missing evidence, and recommended next steps.

## How To Invoke The Skill

In future prompts, explicitly mention the skill name when you want Codex to apply it:

```text
请使用 yk-ferta-clinical-product skill，继续设计这个临床工作流。
```

or:

```text
Use the yk-ferta-clinical-product skill to review this diagnosis workflow.
```

If the task clearly concerns yk-FERTA clinical product design, Codex should also read and apply the skill even if the user does not explicitly mention it.

## Engineering Guidance

- Prefer changes under `src/yk_ferta`, `docs`, `scripts`, `tests`, and `skills`.
- Keep original DeepRare files as reference unless explicitly refactoring or wrapping them.
- Avoid leaking API keys, private patient identifiers, or raw sensitive case data into committed code, docs, logs, or examples.
- Preserve existing user changes. Do not revert unrelated files.
- For code edits, add or update tests when behavior changes.
- For clinical reasoning changes, update the relevant docs or skill if the change reflects a product-level principle.

## Clinical Reasoning Guardrails

- Do not present a gene-specific cause as confirmed unless the current patient has patient-specific molecular evidence.
- Do not treat similar private testing cases as final-diagnosis cases.
- Do not solve one noisy demo case by adding brittle disease-specific hard-coded rules.
- Keep uncertainty visible in final outputs.
- Make intermediate artifacts inspectable in the Web UI or task result.
