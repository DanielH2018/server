from renderer.renderer_lib.documents import parse_documents


def test_a_yaml_stream_parses_each_mapping_document_beside_itself():
    stream = "kind: ConfigMap\nmetadata: {name: a}\n---\n---\n- not a mapping\n"
    ((obj, doc),) = parse_documents([stream])
    assert (obj.kind, obj.name) == ("ConfigMap", "a")
    assert doc == {"kind": "ConfigMap", "metadata": {"name": "a"}}
