from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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
