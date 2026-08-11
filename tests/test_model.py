from __future__ import annotations

import pytest
from conftest import fixture

from obicfg.errors import ValidationError
from obicfg.model import parse_menu, parse_page


@pytest.fixture
def service():
    return parse_page("EXAMPLE_SVC_", fixture("EXAMPLE_SVC_.xml"))


def test_page_metadata(service):
    assert service.name == "EXAMPLE_SVC_"
    assert service.title == "Example Service"
    assert service.reboot_required is False
    assert service.filename == "EXAMPLE_SVC_.xml"


def test_absent_current_means_still_at_default(service):
    port = service.get("ProxyServerPort")
    assert port.current is None
    assert port.is_default is True
    # The effective value is the default, not an empty string.
    assert port.value == "5060"


def test_present_current_overrides_default(service):
    enable = service.get("Enable")
    assert enable.default == "true"
    assert enable.value == "false"
    assert enable.is_default is False


def test_entities_are_decoded(service):
    # The device XML-escapes < and > inside call-route values.
    assert service.get("X_InboundCallRoute").value == "{(<**1:>(Msp1)):sp1}"


def test_lookup_is_case_insensitive_and_hash_addressable(service):
    assert service.get("enable").hash == "aaaa0001"
    assert service.by_hash("aaaa0001").name == "Enable"
    assert service.get("nope") is None


def test_read_only_parameters_are_flagged(service):
    assert service.get("RegistrationState").writable is False
    with pytest.raises(ValidationError, match="read-only"):
        service.get("RegistrationState").coerce("Anything")


def test_duplicate_names_across_objects_are_reachable():
    system = parse_page("EXAMPLE_SYS_", fixture("EXAMPLE_SYS_.xml"))
    # Bare name finds the first; the qualified form reaches the second.
    assert system.get("MACAddress").value == "000000000001"
    assert system.get("Product Information.MACAddress").value == "000000000002"


class TestCoerce:
    def test_bool_accepts_python_and_text(self, service):
        enable = service.get("Enable")
        assert enable.coerce(True) == "true"
        assert enable.coerce("yes") == "true"
        assert enable.coerce("OFF") == "false"
        with pytest.raises(ValidationError, match="boolean"):
            enable.coerce("maybe")

    def test_enumeration_is_canonicalised(self, service):
        transport = service.get("ProxyServerTransport")
        assert transport.coerce("tls") == "TLS"
        with pytest.raises(ValidationError, match="not one of"):
            transport.coerce("SCTP")

    def test_integer_range(self, service):
        port = service.get("ProxyServerPort")
        assert port.coerce(5061) == "5061"
        assert port.coerce(" 5062 ") == "5062"
        with pytest.raises(ValidationError, match="above maximum"):
            port.coerce(70000)
        with pytest.raises(ValidationError, match="integer"):
            port.coerce("5060x")

    def test_max_length(self, service):
        name = service.get("CallerIDName")
        assert name.coerce("sixteen chars ok") == "sixteen chars ok"
        with pytest.raises(ValidationError, match="maximum is 16"):
            name.coerce("this one is far too long")

    def test_literal_default_is_reserved(self, service):
        # The device overloads "default" as reset-to-factory, so it can never
        # be stored as a value. Catch it here rather than silently wiping.
        with pytest.raises(ValidationError, match="reserved"):
            service.get("CallerIDName").coerce("default")


def test_parse_menu_keeps_order_and_dedupes():
    entries = parse_menu(fixture("menu.htm"))
    assert entries == [
        ("EXAMPLE_SYS_.xml", "System Status"),
        ("EXAMPLE_SVC_.xml", "Example Service"),
    ]


class TestParsingEdges:
    def test_integer_below_the_minimum(self, service):
        with pytest.raises(ValidationError, match="below minimum"):
            service.get("ProxyServerPort").coerce(-1)

    def test_a_parameter_with_no_syntax_block_is_a_plain_string(self):
        page = parse_page(
            "X",
            '<model><object name="O"><parameter name="P">'
            '<value hash="1" type="input" default=""/></parameter></object></model>',
        )
        assert page.get("P").syntax.kind == "string"
        assert page.get("P").coerce("anything") == "anything"

    def test_an_empty_syntax_block_falls_back_to_string(self):
        page = parse_page(
            "X",
            '<model><object name="O"><parameter name="P"><syntax></syntax>'
            '<value hash="1" type="input" default=""/></parameter></object></model>',
        )
        assert page.get("P").syntax.kind == "string"

    def test_non_numeric_range_attributes_are_ignored(self):
        page = parse_page(
            "X",
            '<model><object name="O"><parameter name="P"><syntax><unsignedInt>'
            '<range minInclusive="lots" maxInclusive="more"/></unsignedInt></syntax>'
            '<value hash="1" type="input" default="0"/></parameter></object></model>',
        )
        assert page.get("P").syntax.min_inclusive is None
        assert page.get("P").coerce(999) == "999"

    def test_the_xml_suffix_is_accepted_on_the_page_name(self):
        assert parse_page("EXAMPLE_SVC_.xml", fixture("EXAMPLE_SVC_.xml")).name == "EXAMPLE_SVC_"

    def test_display_only_rows_without_a_hash_are_skipped(self):
        page = parse_page(
            "X",
            '<model><object name="O">'
            '<parameter name="Label"><value type="input" default=""/></parameter>'
            '<parameter name="Real"><value hash="1" type="input" default=""/></parameter>'
            "</object></model>",
        )
        assert [p.name for p in page.parameters] == ["Real"]

    def test_parse_objects_skips_rows_with_no_value_element(self):
        from obicfg.model import parse_objects

        objects = parse_objects(
            '<model><object name="O">'
            "<parameter name=\"NoValue\"><description>x</description></parameter>"
            '<parameter name="Has"><value hash="1" default="d"/></parameter>'
            "</object></model>"
        )
        assert objects == [("O", {"Has": "d"})]


class TestAmbiguousNames:
    PAGE = (
        '<model><object name="G711U Codec">'
        '<parameter name="BitRate" access="readOnly"><value hash="1" default="64000"/></parameter>'
        '<parameter name="Enable"><value hash="2" type="bool" default="true"/></parameter>'
        "</object>"
        '<object name="iLBC Codec">'
        '<parameter name="BitRate"><value hash="3" default="13333"/></parameter>'
        '<parameter name="Enable"><value hash="4" type="bool" default="false"/></parameter>'
        "</object></model>"
    )

    def test_candidates_returns_every_match_in_order(self):
        page = parse_page("CODEC", self.PAGE)
        matches = page.candidates("BitRate")
        assert [m.obj for m in matches] == ["G711U Codec", "iLBC Codec"]
        assert [m.writable for m in matches] == [False, True]

    def test_a_qualified_name_selects_exactly_one(self):
        page = parse_page("CODEC", self.PAGE)
        assert page.candidates("iLBC Codec.BitRate")[0].hash == "3"
        assert len(page.candidates("iLBC Codec.BitRate")) == 1

    def test_an_unknown_name_has_no_candidates(self):
        assert parse_page("CODEC", self.PAGE).candidates("Nope") == []

    def test_the_writable_one_is_not_the_first(self):
        # This is why first-match-wins was wrong: `set CODEC.BitRate=X` hit a
        # read-only G711U parameter while the user meant iLBC.
        page = parse_page("CODEC", self.PAGE)
        assert page.get("BitRate").writable is False
        assert page.candidates("iLBC Codec.BitRate")[0].writable is True


def test_an_over_long_value_is_allowed_where_the_device_already_exceeds_its_own_limit():
    # Real case: OBiPLUS ships an inbound route of 276 characters on a page
    # declaring a 128 maximum. Refusing would make it uneditable, including
    # writing back what is already there.
    page = parse_page(
        "X",
        '<model><object name="O"><parameter name="P"><syntax><string>'
        '<size maxLength="4"/></string></syntax>'
        '<value hash="1" type="input" default="" current="far too long"/>'
        "</parameter></object></model>",
    )
    assert page.get("P").coerce("also too long") == "also too long"


def test_an_over_long_value_is_refused_when_the_limit_is_being_honoured():
    page = parse_page(
        "X",
        '<model><object name="O"><parameter name="P"><syntax><string>'
        '<size maxLength="4"/></string></syntax>'
        '<value hash="1" type="input" default="ok"/>'
        "</parameter></object></model>",
    )
    with pytest.raises(ValidationError, match="maximum is 4"):
        page.get("P").coerce("too long")
