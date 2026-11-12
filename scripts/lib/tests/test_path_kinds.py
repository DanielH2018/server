from lib.path_kinds import is_doc, is_test_only_path


def test_a_markdown_file_is_a_doc_and_a_template_is_not():
    assert is_doc("docs/landing.md")
    assert not is_doc("ansible/roles/k8s/docs/templates/configmap.yaml.j2")


def test_a_path_under_a_tests_directory_is_test_only():
    assert is_test_only_path("ansible/tests/_helpers.py")
    assert is_test_only_path("hosts/components/deploy-ui/tests/_fakes.py")


def test_a_test_module_or_conftest_beside_code_is_test_only():
    assert is_test_only_path("scripts/conftest.py")
    assert is_test_only_path("scripts/dev/test_new_thing.py")


def test_a_shipped_module_is_not_test_only():
    assert not is_test_only_path("ansible/roles/k8s/monitor-bridge/files/registry.py")
    assert not is_test_only_path("scripts/deploy_tools/testing_notes.py")
