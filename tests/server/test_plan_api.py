"""Output settings and the export summary share the command line's planner."""

from fastapi.testclient import TestClient

API = "/api/v1"


def test_colour_choices_use_the_engine_registries(client: TestClient) -> None:
    from mcrawsome.colour.encoding import LEGACY, LOG_GAMUTS
    from mcrawsome.colour.spaces import GAMUTS
    from mcrawsome.colour.transfer import LOGC3_EI, TRANSFERS

    response = client.get(f"{API}/colour")
    assert response.status_code == 200
    choices = response.json()
    assert choices["gamuts"] == [
        {"id": key, "name": " / ".join(value.names) if value.names else value.name}
        for key, value in GAMUTS.items()
    ]
    assert choices["transfers"] == [{"id": key, "name": value.name} for key, value in TRANSFERS.items()]
    assert choices["log_transfers"] == list(LOG_GAMUTS)
    assert choices["logc3_ei"] == list(LOGC3_EI)
    assert choices["legacy"] == {
        key: {"gamut": value.gamut, "transfer": value.transfer} for key, value in LEGACY.items()
    }
