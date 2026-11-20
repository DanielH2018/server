from prom_envelope import prom_result


def test_an_absent_data_or_result_reads_as_no_series():
    assert prom_result({"status": "success"}) == []
    assert prom_result({"status": "success", "data": {}}) == []
