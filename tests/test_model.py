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
