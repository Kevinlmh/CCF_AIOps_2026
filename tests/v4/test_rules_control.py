import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('control',Path(__file__).resolve().parents[2]/'scripts/v4_rules_control.py')


def api():
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def metric(cpu=1,memory=.93,io=0,load=0):
    return dict(cpu_usage=cpu,memory_available_ratio=memory,disk_io_util=io,load1=load)


def test_resource_control_uses_observed_mechanisms_and_keeps_secondary_candidates():
    control=api()
    reference=metric()
    assert control.resource_category([metric(cpu=40,load=2)]*3,reference)=='cpu_pressure'
    assert control.resource_category([metric(cpu=25,memory=.85,load=1)]*3,reference)=='memory_pressure'
    assert control.resource_category([metric(cpu=70,io=99,load=4)]*3,reference)=='disk_io_pressure'
    assert control.resource_category([metric()]*3,reference) is None
    assert control.resource_category([metric(cpu=40,load=2)],reference) is None
