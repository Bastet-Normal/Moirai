"""Regression evidence that UI QA cannot write the real library/configuration."""

import os
import subprocess
import sys
from pathlib import Path

from config.settings import get_settings


def test_custom_words_follow_current_output_root(tmp_path, monkeypatch):
    from config.compliance_words import get_custom_words, save_custom_words

    try:
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "first"))
        get_settings.cache_clear()
        save_custom_words({"test": ["isolated"]})
        assert get_custom_words() == {"test": ["isolated"]}
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "second"))
        get_settings.cache_clear()
        assert get_custom_words() == {}
        save_custom_words({"other": ["separate"]})
        assert (tmp_path / "first" / ".custom_compliance.json").exists()
        assert (tmp_path / "second" / ".custom_compliance.json").exists()
    finally:
        get_settings.cache_clear()


def test_offline_demo_isolates_config_words_projects_and_blocks_transport(tmp_path):
    code = '''
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from scripts.serve_demo import demo_environment
from core.llm import get_llm_client
from core.state_manager import StateManager
from pipeline.novel_pipeline import NovelPipeline
from config.settings import get_settings

root = Path(sys.argv[1])
with demo_environment(root) as app, TestClient(app) as client:
    assert get_settings().OPENAI_API_KEY == 'offline-demo-not-a-real-key'
    assert get_settings().OUTPUT_DIR == str(root / 'projects')
    assert len(client.get('/api/novels').json()['novels']) == 2
    assert client.get('/').status_code == 200
    result = client.post('/api/config', json={'base_url':'https://example.invalid/v1', 'model':'changed'})
    assert result.status_code == 200, result.text
    assert 'changed' in (root / '.env').read_text()
    from config.compliance_words import save_custom_words
    save_custom_words({'test':['isolated']})
    assert (root / 'projects' / '.custom_compliance.json').exists()

    manager = StateManager('demo_alpha')
    original_text = {
        number: chapter.full_text
        for number, chapter in manager.state.chapters.items()
    }
    manager.initialize_history_repair({}, '离线测试已核对章前空状态')
    pipeline = NovelPipeline('demo_alpha')
    for number in (1, 2):
        delta = pipeline.reconcile_existing_chapter_state(number)
        assert delta.summary_of_events == f'离线验收：第 {number} 章状态已提取'
    repaired = StateManager('demo_alpha')
    assert repaired.get_timeline_status()['complete'] is True
    assert repaired.get_state_reconciliation_status()['chapters'] == []
    assert {
        number: chapter.full_text
        for number, chapter in repaired.state.chapters.items()
    } == original_text

    created = client.post('/api/novel/create', json={
        'name': 'offline_created',
        'idea': '守夜人在断电星港收到一封来自失踪航路的纸质来信',
        'genre': '科幻悬疑',
        'chapters': 1,
        'volumes': 1,
    })
    assert created.status_code == 200, created.text
    status = client.get('/api/status').json()
    assert status['current_novel'] == 'offline_created'
    assert status['is_running'] is False
    assert status['current_step'] == '全部生成完成'
    assert status['progress'] == 100
    assert status['error'] == ''
    generated = StateManager('offline_created')
    assert generated.state.pending_initial_planning is None
    assert generated.state.pending_outline_plan is None
    assert generated.state.concept.title == '离线星港档案'
    assert generated.state.world_setting.world_name == '回声星港'
    assert len(generated.state.characters) == 3
    assert generated.state.global_outline.total_volumes == 1
    assert len(generated.state.global_outline.volumes[0].chapters) == 10
    assert generated.state.current_chapter_idx == 1
    assert generated.state.chapters[1].audit_history[-1].passed is True
    assert generated.get_state_reconciliation_status()['chapters'] == []
    assert (generated.multimodal_dir / 'audio_drama_package.json').is_file()
    assert (generated.multimodal_dir / 'short_drama_chap_001.json').is_file()
    assert len(client.get('/api/novels').json()['novels']) == 3

    continued = client.post('/api/novel/continue', json={
        'name': 'offline_created', 'count': 2, 'expected_revision': generated.revision,
    })
    assert continued.status_code == 200, continued.text
    status = client.get('/api/status').json()
    assert status['current_step'] == '全部生成完成'
    assert status['error'] == ''
    generated = StateManager('offline_created')
    assert generated.state.current_chapter_idx == 3
    assert sorted(generated.state.chapters) == [1, 2, 3]
    assert generated.state.delivery_package is not None
    assert '全港断电后' in generated.state.delivery_package.tiktok_promo_script
    assert (generated.delivery_dir / 'delivery_package.json').is_file()
    assert (generated.multimodal_dir / 'short_drama_chap_002.json').is_file()

    generated = StateManager('offline_created')
    reader = client.post('/api/novel/reader_simulate', json={
        'name': 'offline_created', 'chapter': 2, 'expected_revision': generated.revision,
    })
    assert reader.status_code == 200, reader.text
    assert reader.json()['status'] == 'started'
    assert client.get('/api/status').json()['current_step'] == '读者推演完成'
    generated = StateManager('offline_created')
    assert generated.reader_simulations[2].overall_sentiment_summary.startswith('离线读者认为')
    branched = client.post('/api/novel/branch_simulate', json={
        'name': 'offline_created', 'chapter': 2, 'expected_revision': generated.revision,
    })
    assert branched.status_code == 200, branched.text
    assert branched.json()['status'] == 'started'
    assert client.get('/api/status').json()['current_step'] == '剧情分支推演完成'
    generated = StateManager('offline_created')
    assert 2 in generated.reader_simulations
    assert 2 in generated.branch_simulations

    outlined = client.patch('/api/novel/offline_created/outline/2', json={
        'title': '重写后的第二封信', 'expected_revision': generated.revision,
    })
    assert outlined.status_code == 200, outlined.text
    generated = StateManager('offline_created')
    assert generated.state.delivery_package is not None
    assert 2 not in generated.branch_simulations
    assert 2 in generated.reader_simulations
    assert 2 in generated.short_drama_scripts
    before_rewrite = generated.revision
    regenerated = client.post('/api/novel/regenerate', json={
        'name': 'offline_created', 'chapter': 2, 'expected_revision': before_rewrite,
    })
    assert regenerated.status_code == 200, regenerated.text
    status = client.get('/api/status').json()
    assert status['current_step'] == '重写完成'
    generated = StateManager('offline_created')
    assert generated.revision != before_rewrite
    assert generated.state.delivery_package is None
    assert not (generated.delivery_dir / 'delivery_package.json').exists()
    assert 2 not in generated.short_drama_scripts
    assert 2 not in generated.reader_simulations
    assert 2 not in generated.branch_simulations
    assert not (generated.multimodal_dir / 'short_drama_chap_002.json').exists()
    assert not (generated.reader_sim_dir / 'reader_sim_chap_002.json').exists()

    rebuilt_drama = client.post('/api/novel/offline_created/short_drama/2/generate', json={
        'expected_revision': generated.revision,
    })
    assert rebuilt_drama.status_code == 200, rebuilt_drama.text
    status = client.get('/api/status').json()
    assert status['current_step'] == '短剧分镜生成完成'
    generated = StateManager('offline_created')
    assert generated.short_drama_scripts[2].title.endswith('重写后的第二封信')

    rebuilt_audio = client.post('/api/novel/offline_created/audio_package/generate', json={
        'expected_revision': generated.revision,
    })
    assert rebuilt_audio.status_code == 200, rebuilt_audio.text
    status = client.get('/api/status').json()
    assert status['current_step'] == '有声演播配置生成完成'
    generated = StateManager('offline_created')
    assert generated.audio_package.novel_title == '离线星港档案'

    delivered = client.post('/api/novel/offline_created/generate_delivery', json={
        'expected_revision': generated.revision,
    })
    assert delivered.status_code == 200, delivered.text
    status = client.get('/api/status').json()
    assert status['current_step'] == '商业交付包生成完成'
    generated = StateManager('offline_created')
    assert generated.state.delivery_package is not None
    assert generated.state.delivery_package.cost_and_token_estimate['total_words_generated'] > 0

    try:
        get_llm_client().generate_text('test', 'test')
    except RuntimeError as exc:
        assert '离线演示模式' in str(exc)
    else:
        raise AssertionError('demo must never invoke a provider')
'''
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "demo")],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=45, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
