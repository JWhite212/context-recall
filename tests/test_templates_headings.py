from src.templates import TemplateManager


def test_standard_prompt_uses_split_questions_and_risks():
    tpl = TemplateManager().get_template("standard")
    prompt = tpl.system_prompt
    assert "## Open questions" in prompt
    assert "## Risks and blockers" in prompt
    assert "## Open Questions & Risks" not in prompt
    assert "## Executive summary" in prompt
    assert "## Decisions made" in prompt
    assert "## Discussion points" in prompt
    assert "## Notable quotes" in prompt


def test_standard_template_sections_updated():
    tpl = TemplateManager().get_template("standard")
    assert "Executive summary" in tpl.sections
    assert "Decisions made" in tpl.sections
    assert "Open questions" in tpl.sections
    assert "Risks and blockers" in tpl.sections
    # Writer-owned sections still requested from the LLM for extraction quality.
    assert "Participants" in tpl.sections and "Tags" in tpl.sections


def test_no_em_dash_in_standard_prompt():
    assert "—" not in TemplateManager().get_template("standard").system_prompt
