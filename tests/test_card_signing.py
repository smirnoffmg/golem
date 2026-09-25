import base64
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from test_edge_cards import card

from golem.edge.__main__ import load_public_cards
from golem.edge.card_signing import (
    ALGORITHM,
    CardRejected,
    CardVerified,
    card_keys,
    sign_card,
    verify_card,
)
from golem.run_token import SigningKey

KEY = SigningKey.generate("cards-1")
OTHER = SigningKey.generate("cards-1")
JKU = "https://golem.example.test/.well-known/golem-card-keys.json"
EXAMPLES = Path(__file__).parent.parent / "examples"


def served(key: SigningKey = KEY) -> dict[str, Any]:
    """The card as the edge serves it: signed, then serialized."""
    return agent_card_to_dict(sign_card(card(), key, jku=JKU))


def protected_header(card_json: dict[str, Any]) -> dict[str, Any]:
    [signature] = card_json["signatures"]
    encoded = signature["protected"]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def test_a_signed_card_verifies_against_the_published_keys() -> None:
    assert verify_card(served(), card_keys([KEY])) == CardVerified(kid="cards-1")


def test_the_protected_header_names_the_algorithm_type_key_and_key_set() -> None:
    assert protected_header(served()) == {
        "alg": ALGORITHM,
        "typ": "JOSE",
        "kid": "cards-1",
        "jku": JKU,
    }


def test_signing_leaves_the_unsigned_card_alone() -> None:
    unsigned = card()

    sign_card(unsigned, KEY, jku=JKU)

    assert list(unsigned.signatures) == []


def test_signing_a_signed_card_replaces_its_signature() -> None:
    twice = sign_card(sign_card(card(), KEY, jku=JKU), KEY, jku=JKU)

    assert len(twice.signatures) == 1


@pytest.mark.parametrize(
    "tamper",
    [
        lambda c: c.update(name="evil"),
        lambda c: c.update(description="Does whatever you ask."),
        lambda c: c.update(version="9.9.9"),
        lambda c: c["supportedInterfaces"][0].update(url="https://attacker.example.test/a2a"),
        lambda c: c["supportedInterfaces"][0].update(tenant="reviewer"),
        lambda c: c["capabilities"].update(streaming=True),
        lambda c: c["securitySchemes"]["oidc"]["openIdConnectSecurityScheme"].update(
            openIdConnectUrl="https://attacker.example.test/.well-known/openid-configuration"
        ),
        lambda c: c["skills"][0].update(id="exfiltrate"),
        lambda c: c["skills"][0]["tags"].append("admin"),
        lambda c: c["defaultOutputModes"].append("application/octet-stream"),
        lambda c: c.update(documentationUrl="https://attacker.example.test/docs"),
    ],
    ids=[
        "name",
        "description",
        "version",
        "interface-url",
        "tenant",
        "capabilities",
        "oidc-url",
        "skill-id",
        "skill-tags",
        "output-modes",
        "added-field",
    ],
)
def test_a_tampered_card_fails(tamper: Any) -> None:
    tampered = copy.deepcopy(served())
    tamper(tampered)

    assert isinstance(verify_card(tampered, card_keys([KEY])), CardRejected)


def test_a_card_signed_by_another_key_with_the_same_kid_fails() -> None:
    assert isinstance(verify_card(served(OTHER), card_keys([KEY])), CardRejected)


def test_a_card_signed_by_a_key_not_in_the_set_fails() -> None:
    stranger = SigningKey.generate("stranger")

    result = verify_card(served(stranger), card_keys([KEY]))

    assert isinstance(result, CardRejected)


def test_the_signatures_field_is_not_part_of_what_is_signed() -> None:
    # A2A 1.0, 8.4.1: the signatures field is excluded, so a second signature added later
    # (a rotation overlap) leaves the first one valid.
    both = served()
    both["signatures"] += served(SigningKey.generate("cards-2"))["signatures"]

    assert verify_card(both, card_keys([KEY])) == CardVerified(kid="cards-1")


def test_any_valid_signature_among_several_is_enough_during_a_rotation() -> None:
    new = SigningKey.generate("cards-2")
    both = served()
    both["signatures"] += served(new)["signatures"]

    assert verify_card(both, card_keys([new])) == CardVerified(kid="cards-2")


def test_an_unsigned_card_fails() -> None:
    unsigned = agent_card_to_dict(card())

    assert verify_card(unsigned, card_keys([KEY])) == CardRejected("the card has no signature")


def test_a_signature_with_another_algorithm_fails() -> None:
    forged = served()
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JOSE", "kid": "cards-1"}).encode()
    )
    forged["signatures"][0]["protected"] = header.decode().rstrip("=")
    forged["signatures"][0]["signature"] = ""

    assert isinstance(verify_card(forged, card_keys([KEY])), CardRejected)


def test_the_key_set_url_in_the_header_is_never_followed() -> None:
    # A card may name any jku; the verifier trusts only the keys it was given (8.4.3).
    attacker = SigningKey.generate("cards-1")
    signed = agent_card_to_dict(
        sign_card(card(), attacker, jku="https://attacker.example.test/keys.json")
    )

    assert isinstance(verify_card(signed, card_keys([KEY])), CardRejected)


@pytest.mark.parametrize(
    "keys",
    [{}, {"keys": "none"}, {"keys": [{"kty": "EC"}]}],
    ids=["empty", "not-a-list", "incomplete-key"],
)
def test_a_malformed_key_set_rejects_rather_than_raises(keys: dict[str, Any]) -> None:
    assert isinstance(verify_card(served(), keys), CardRejected)


def test_a_malformed_card_rejects_rather_than_raises() -> None:
    assert isinstance(verify_card({"name": 7, "signatures": "x"}, card_keys([KEY])), CardRejected)


def test_the_published_key_set_holds_public_keys_only() -> None:
    [key] = card_keys([KEY])["keys"]

    assert (key["kid"], key["alg"], key["use"], key["kty"], key["crv"]) == (
        "cards-1",
        "ES256",
        "sig",
        "EC",
        "P-256",
    )
    assert "d" not in key


def test_the_edge_signs_every_card_it_loads_from_the_catalogs() -> None:
    cards = load_public_cards(
        EXAMPLES,
        base_url="https://golem.example.test",
        oidc_discovery_url="https://idp.example.test/.well-known/openid-configuration",
        signing_key=KEY,
    )

    assert cards
    for loaded in cards.values():
        assert len(loaded.signatures) == 1
        assert verify_card(agent_card_to_dict(loaded), card_keys([KEY])) == CardVerified("cards-1")
        assert protected_header(agent_card_to_dict(loaded))["jku"] == JKU
