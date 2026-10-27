"""The home-assistant.io page draft: frontmatter, sections, and the scope of the first core PR.

The page lives at ``docs/home-assistant-io/nighp_baby_tracker.markdown`` and is copied into
``source/_integrations/`` of the docs repository when the core PR opens. These tests check the
parts a docs reviewer or the docs build would reject, and that the page stays inside what the first
core PR ships: the user config flow, the coordinator, and the Last diaper, Last feed, and Last
sleep sensors.
"""

import json
import re
from typing import Any

import pytest
import yaml

from .helpers import INTEGRATION, ROOT

PAGE = ROOT / "docs" / "home-assistant-io" / "nighp_baby_tracker.markdown"

# The manifest spells the IoT class in snake case; the docs page spells it out.
IOT_CLASSES = {
    "cloud_polling": "Cloud Polling",
    "cloud_push": "Cloud Push",
    "local_polling": "Local Polling",
    "local_push": "Local Push",
    "calculated": "Calculated",
    "assumed_state": "Assumed State",
}

# The keys the docs repo's template and a recent cloud-polling page (ohme) carry, plus the Gold
# documentation rules' quality scale key.
REQUIRED_KEYS = (
    "title",
    "description",
    "ha_categories",
    "ha_release",
    "ha_iot_class",
    "ha_config_flow",
    "ha_codeowners",
    "ha_domain",
    "ha_platforms",
    "ha_integration_type",
    "ha_quality_scale",
)

QUALITY_SCALES = {"bronze", "silver", "gold", "platinum", "internal", "legacy"}

# The first core PR ships the sensor platform and nothing else: diagnostics is PR 3.
FIRST_PR_PLATFORMS = ["sensor"]


def _page_text() -> str:
    return PAGE.read_text() if PAGE.exists() else ""


def _split() -> tuple[dict[str, Any], str]:
    """Return the frontmatter and the body, or an empty frontmatter when the page has none."""
    match = re.match(r"---\n(.*?)\n---\n(.*)", _page_text(), re.DOTALL)
    if match is None:
        return {}, _page_text()
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def _manifest() -> dict[str, Any]:
    return json.loads((INTEGRATION / "manifest.json").read_text())


def _strings() -> dict[str, Any]:
    return json.loads((INTEGRATION / "strings.json").read_text())


def _without_comments(body: str) -> str:
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def test_the_page_is_not_empty() -> None:
    assert _page_text().strip() != ""


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_the_frontmatter_has_every_required_key(key: str) -> None:
    frontmatter, _ = _split()
    assert key in frontmatter, f"missing frontmatter key {key}"


def test_the_frontmatter_has_no_retired_category_key() -> None:
    # The docs build rejects the old `ha_category`: it is `ha_categories` now.
    frontmatter, _ = _split()
    assert frontmatter != {}
    assert "ha_category" not in frontmatter


def test_the_domain_matches_the_manifest() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("ha_domain") == _manifest()["domain"]


def test_the_title_is_the_manifest_name() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("title") == _manifest()["name"]


def test_the_codeowners_match_the_manifest() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("ha_codeowners") == _manifest()["codeowners"]


def test_the_iot_class_matches_the_manifest() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("ha_iot_class") == IOT_CLASSES[_manifest()["iot_class"]]


def test_the_config_flow_flag_matches_the_manifest() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("ha_config_flow") is _manifest()["config_flow"]


def test_the_integration_type_matches_the_manifest() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("ha_integration_type") == _manifest()["integration_type"]


def test_the_platforms_are_only_the_first_prs() -> None:
    frontmatter, _ = _split()
    platforms = frontmatter.get("ha_platforms")
    assert isinstance(platforms, list)
    assert platforms == FIRST_PR_PLATFORMS


def test_the_categories_are_one_to_three() -> None:
    frontmatter, _ = _split()
    categories = frontmatter.get("ha_categories")
    assert isinstance(categories, list)
    assert 1 <= len(categories) <= 3


def test_the_release_is_a_year_and_month() -> None:
    frontmatter, _ = _split()
    assert re.fullmatch(r"\d{4}\.\d{1,2}", str(frontmatter.get("ha_release")))


def test_the_quality_scale_is_a_known_tier() -> None:
    frontmatter, _ = _split()
    assert frontmatter.get("ha_quality_scale") in QUALITY_SCALES


def test_the_description_is_one_line() -> None:
    frontmatter, _ = _split()
    description = frontmatter.get("description")
    assert isinstance(description, str)
    assert description.strip() != ""
    assert "\n" not in description.strip()


def test_the_page_names_its_template_source_at_the_top() -> None:
    _, body = _split()
    top = body.lstrip().split("\n\n", 1)[0]
    assert top.startswith("<!--")
    assert "source/_integrations/_integration_docs_template.markdown" in top
    assert re.search(r"\b[0-9a-f]{40}\b", top), "the comment names no commit"


def test_the_page_uses_the_config_flow_include() -> None:
    _, body = _split()
    assert "{% include integrations/config_flow.md %}" in _without_comments(body)


def test_the_page_uses_the_removal_include() -> None:
    _, body = _split()
    assert "{% include integrations/remove_device_service.md %}" in _without_comments(body)


def test_the_page_has_no_actions_triggers_or_conditions_include() -> None:
    # The first core PR registers none of them.
    _, body = _split()
    live = _without_comments(body)
    assert live.strip() != ""
    assert "{% include integrations/actions.md %}" not in live
    assert "{% include integrations/triggers.md %}" not in live
    assert "{% include integrations/conditions.md %}" not in live
    assert "{% include integrations/triggers_conditions_actions.md %}" not in live


@pytest.mark.parametrize(
    "heading",
    [
        "## Supported devices",
        "## Prerequisites",
        "## Supported functionality",
        "### Sensors",
        "## Baby Tracker (Nighp) automation examples",
        "## Data updates",
        "## Known limitations",
        "## Troubleshooting",
        "## Removing the integration",
    ],
)
def test_the_page_has_the_templates_headings(heading: str) -> None:
    _, body = _split()
    assert re.search(rf"^{re.escape(heading)}$", body, re.MULTILINE), f"missing {heading}"


def test_the_page_has_no_h1_heading() -> None:
    _, body = _split()
    assert re.search(r"^# ", body, re.MULTILINE) is None
    assert body.strip() != ""


def test_the_sections_come_in_the_templates_order() -> None:
    _, body = _split()
    order = [
        "## Supported devices",
        "## Prerequisites",
        "{% include integrations/config_flow.md %}",
        "## Supported functionality",
        "## Baby Tracker (Nighp) automation examples",
        "## Data updates",
        "## Known limitations",
        "## Troubleshooting",
        "## Removing the integration",
    ]
    positions = [_without_comments(body).find(item) for item in order]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions)


def test_the_sensors_use_the_names_in_strings_json() -> None:
    _, body = _split()
    names = _strings()["entity"]["sensor"]
    for key in ("last_diaper", "last_feed", "last_sleep"):
        assert f"**{names[key]['name']}**" in body


def test_the_setup_fields_use_the_labels_in_strings_json() -> None:
    _, body = _split()
    data = _strings()["config"]["step"]["user"]["data"]
    for label in data.values():
        assert re.search(rf"^{label}:$", body, re.MULTILINE), f"no configuration entry {label}"


def test_the_page_does_not_describe_later_pr_features() -> None:
    _, body = _split()
    live = _without_comments(body).lower()
    assert live.strip() != ""
    later = (
        r"\bbuttons?\b",
        r"\bswitch(es)?\b",
        r"binary.sensor",
        r"\bpump(?!ed)",  # a pumped-milk feed is a first-PR value
        r"\btimers?\b",
        r"\basleep\b",
        r"\bundo\b",
        r"\blog_\w+",
        r"diagnostics",
        r"reauth",
        r"diapers today",
        r"feeds today",
        r"bottle volume today",
        r"\boptions?\b",
        r"\bblueprint",
        r"live activity",
        r"\brepairs?\b",
    )
    found = [pattern for pattern in later if re.search(pattern, live)]
    assert found == []


def test_the_page_names_no_real_account_or_person() -> None:
    _, body = _split()
    live = _page_text()
    assert re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", live) is None
    # The codeowner handle is the only @-name.
    assert set(re.findall(r"(?<![\w])@\w+", live)) <= set(_manifest()["codeowners"])
    assert body.strip() != ""


def test_the_yaml_examples_parse() -> None:
    _, body = _split()
    blocks = re.findall(r"\{% example %\}\n(.*?)\{% endexample %\}", body, re.DOTALL)
    assert blocks, "the page has no YAML example"
    for block in blocks:
        _, _, source = block.partition("automation: |\n")
        assert source, "an example block is not an automation"
        automation = yaml.safe_load("\n".join(line[2:] for line in source.splitlines()))
        assert automation["triggers"]
        assert automation["actions"]


def test_the_removal_section_says_what_is_deleted() -> None:
    _, body = _split()
    removal = body.split("## Removing the integration", 1)[-1]
    assert "{% include integrations/remove_device_service.md %}" in removal
    assert "sync state" in removal


def test_the_data_updates_section_states_the_interval_in_the_code() -> None:
    from custom_components.nighp_baby_tracker.const import SCAN_INTERVAL

    _, body = _split()
    section = body.split("## Data updates", 1)[-1].split("\n## ", 1)[0]
    assert f"{int(SCAN_INTERVAL.total_seconds() // 60)} minutes" in section


def test_the_troubleshooting_section_covers_the_sign_in_errors() -> None:
    _, body = _split()
    section = body.split("## Troubleshooting", 1)[-1].split("\n## ", 1)[0]
    errors = _strings()["config"]["error"]
    assert errors["invalid_auth"] in section
    assert errors["cannot_connect"] in section


def test_the_page_says_how_to_refresh_sooner_than_the_interval() -> None:
    _, body = _split()
    section = body.split("## Data updates", 1)[-1].split("\n## ", 1)[0]
    assert "homeassistant.update_entity" in section


def test_the_page_does_not_document_diagnostics() -> None:
    # Diagnostics is the third core PR.
    _, body = _split()
    live = _without_comments(body)
    assert live.strip() != ""
    assert "diagnostics" not in live.lower()


def test_the_page_does_not_document_reauthentication() -> None:
    # Reauthentication is the second core PR: the first promises no sign-in prompt.
    _, body = _split()
    live = _without_comments(body).lower()
    assert live.strip() != ""
    for phrase in ("reauth", "sign in again", "password changes", "re-authenticate"):
        assert phrase not in live


def test_the_page_does_not_document_sensor_attributes() -> None:
    # The first core PR's sensors have a state and no attributes.
    _, body = _split()
    live = _without_comments(body)
    assert live.strip() != ""
    assert "attribute" not in live.lower()
    for name in (
        "type",
        "note",
        "kind",
        "amount",
        "unit",
        "duration_minutes",
        "left_duration_minutes",
        "right_duration_minutes",
        "both_duration_minutes",
        "finish_side",
    ):
        assert f"`{name}`" not in live
    assert "attributes." not in live


def test_the_page_does_not_document_stale_device_handling() -> None:
    # Removing a deleted baby's device is the sixth core PR.
    _, body = _split()
    live = _without_comments(body)
    assert live.strip() != ""
    assert "Removing a baby" not in live
    assert "device page" not in live


def test_the_source_comment_states_the_scope_and_the_polling_caveat() -> None:
    _, body = _split()
    top = body.lstrip().split("\n\n", 1)[0]
    assert "user config flow, coordinator, Last diaper, Last feed, and Last sleep sensors" in top
    assert "every minute while a timer runs" in top
