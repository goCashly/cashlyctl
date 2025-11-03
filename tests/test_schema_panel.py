import pytest

from cashlyctl.widgets.schemapanel import SchemaPanel


@pytest.fixture()
def sample_schema_payload():
    return {
        "response": {
            "response": [
                {
                    "results": {
                        "results": [
                            {
                                "nodes": [
                                    {"id": "1", "labels": ["User"]},
                                    {"id": "2", "labels": ["Account"]},
                                    {"id": "3", "labels": ["Transaction"]},
                                ],
                                "relationships": [
                                    {
                                        "id": "4",
                                        "type": "OWNS",
                                        "startNode": "1",
                                        "endNode": "2",
                                    },
                                    {
                                        "id": "5",
                                        "type": "INITIATED",
                                        "startNode": "1",
                                        "endNode": "3",
                                    },
                                    {
                                        "id": "6",
                                        "type": "SETTLES",
                                        "startNode": "3",
                                        "endNode": "2",
                                    },
                                ],
                            }
                        ]
                    }
                }
            ]
        }
    }


def test_format_schema_creates_ascii_graph(sample_schema_payload):
    panel = SchemaPanel()
    output = panel._format_schema(sample_schema_payload)

    # Ensure each node label appears in a styled capsule
    assert "User" in output
    assert "Account" in output
    assert "Transaction" in output

    # Relationships should render as arrows with type labels
    assert "[cyan]OWNS[/cyan]" in output
    assert "[cyan]INITIATED[/cyan]" in output
    assert "▶" in output

    # Nodes without outgoing relationships should be explicitly noted
    assert "No outgoing relationships" in output


def test_format_schema_handles_empty_payload():
    panel = SchemaPanel()
    output = panel._format_schema({})
    assert "No schema data" in output
