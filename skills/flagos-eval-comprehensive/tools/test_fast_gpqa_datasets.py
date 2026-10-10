from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import types


TOOLS = Path(__file__).parent
SPEC = spec_from_file_location('fast_gpqa', TOOLS / 'fast_gpqa.py')
FAST_GPQA = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FAST_GPQA)


def test_dataset_registry_and_defaults():
    assert FAST_GPQA.DATASET_CONFIG['gpqa_diamond']['default_limit'] == 50
    assert FAST_GPQA.DATASET_CONFIG['mmlu']['default_limit'] == 20
    assert FAST_GPQA.DATASET_CONFIG['math_500']['default_limit'] == 40
    assert FAST_GPQA.DATASET_CONFIG['mm_bench_en']['default_limit'] == 1000
    assert FAST_GPQA.DATASET_CONFIG['mm_bench_en']['eval_dataset'] == 'mm_bench'
    assert FAST_GPQA.DATASET_CONFIG['mm_bench_en']['subset_list'] == ['en']
    assert FAST_GPQA.DATASET_CONFIG['mm_star']['default_limit'] == 0
    assert FAST_GPQA.DATASET_CONFIG['mm_star']['full_count'] == 1500


def test_generation_config_accepts_model_sampling_fields(tmp_path):
    model_dir = tmp_path / 'model'
    model_dir.mkdir()
    (model_dir / 'generation_config.json').write_text(
        '{"do_sample": true, "temperature": 0.7, "top_p": 0.8, '
        '"top_k": 20, "repetition_penalty": 1.0, "max_tokens": 128}'
    )
    config = FAST_GPQA.resolve_gen_params(False, 4096, str(model_dir))
    assert config['do_sample'] is True
    assert config['temperature'] == 0.7
    assert config['top_p'] == 0.8
    assert config['top_k'] == 20
    assert config['max_tokens'] == 4096


def test_invalid_dataset_names_do_not_fallback():
    assert 'not_a_dataset' not in FAST_GPQA.DATASET_CONFIG


def test_mm_bench_task_config_uses_english_subset_and_default_limit(monkeypatch, tmp_path):
    captured = {}

    class FakeTaskConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    evalscope = types.ModuleType('evalscope')
    evalscope.TaskConfig = FakeTaskConfig
    evalscope.run_task = lambda task_cfg: {'result': {'score': 0.855, 'metrics': [{'num': 1000}]}}
    constants = types.ModuleType('evalscope.constants')
    constants.EvalType = types.SimpleNamespace(OPENAI_API='openai_api')
    monkeypatch.setitem(sys.modules, 'evalscope', evalscope)
    monkeypatch.setitem(sys.modules, 'evalscope.constants', constants)
    monkeypatch.setattr(FAST_GPQA, 'auto_max_tokens', lambda *args, **kwargs: (4096, 32768))
    monkeypatch.setattr(FAST_GPQA, 'check_truncation', lambda *args, **kwargs: (False, 4096))
    monkeypatch.setattr(FAST_GPQA, 'probe_throughput', lambda **kwargs: (2, 0.1))
    monkeypatch.setattr(FAST_GPQA, '_preload_dataset', lambda *args: None)
    monkeypatch.setattr(FAST_GPQA, 'ServiceMonitor', None)
    monkeypatch.chdir(tmp_path)

    report = FAST_GPQA.run_fast_gpqa(
        model_name='Qwen3-VL-4B-Instruct',
        api_base='http://localhost:8000/v1',
        dataset='mm_bench_en',
    )

    assert captured['datasets'] == ['mm_bench']
    assert captured['dataset_args']['mm_bench']['subset_list'] == ['en']
    assert captured['limit'] == 1000
    assert captured['generation_config']['max_tokens'] == 4096
    assert report['total_questions'] == 1000
    assert report['_producer'] == 'fast_gpqa.py'


def test_mm_star_task_config_defaults_to_full_corpus(monkeypatch, tmp_path):
    captured = {}

    class FakeTaskConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    evalscope = types.ModuleType('evalscope')
    evalscope.TaskConfig = FakeTaskConfig
    evalscope.run_task = lambda task_cfg: {'result': {'score': 0.626, 'metrics': [{'num': 1500}]}}
    constants = types.ModuleType('evalscope.constants')
    constants.EvalType = types.SimpleNamespace(OPENAI_API='openai_api')
    monkeypatch.setitem(sys.modules, 'evalscope', evalscope)
    monkeypatch.setitem(sys.modules, 'evalscope.constants', constants)
    monkeypatch.setattr(FAST_GPQA, 'auto_max_tokens', lambda *args, **kwargs: (4096, 32768))
    monkeypatch.setattr(FAST_GPQA, 'check_truncation', lambda *args, **kwargs: (False, 4096))
    monkeypatch.setattr(FAST_GPQA, 'probe_throughput', lambda **kwargs: (2, 0.1))
    monkeypatch.setattr(FAST_GPQA, '_preload_dataset', lambda *args: None)
    monkeypatch.setattr(FAST_GPQA, 'ServiceMonitor', None)
    monkeypatch.chdir(tmp_path)

    report = FAST_GPQA.run_fast_gpqa(
        model_name='Qwen3-VL-4B-Instruct',
        api_base='http://localhost:8000/v1',
        dataset='mm_star',
    )

    assert captured['datasets'] == ['mm_star']
    assert 'limit' not in captured
    assert report['total_questions'] == 1500


def test_mm_bench_preload_uses_huggingface_dataset_loader(monkeypatch):
    calls = []
    datasets = types.ModuleType('datasets')
    datasets.load_dataset = lambda *args, **kwargs: calls.append((args, kwargs))
    monkeypatch.setitem(sys.modules, 'datasets', datasets)

    FAST_GPQA._preload_dataset('modelscope', dataset='mm_bench_en')

    assert len(calls) == 1
    assert calls[0][0] == ('lmms-lab/MMBench',)
    assert calls[0][1]['name'] == 'en'
    assert calls[0][1]['split'] == 'dev'
    assert calls[0][1]['trust_remote_code'] is True
