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
        'name': 'offline_created', 'chapter': 2, 'expected_revision': before_rewrite, 'preview': True,
    })
    assert regenerated.status_code == 200, regenerated.text
    status = client.get('/api/status').json()
    assert status['current_step'] == '重写稿待审阅'
    staged = StateManager('offline_created')
    assert staged.revision == before_rewrite
    assert 2 in staged.short_drama_scripts
    url = '/api/novel/offline_created/rewrite_candidate'
    candidate = client.get(url).json()['candidate']
    assert candidate['task_id'] == regenerated.json()['task_id']
    # A new process reads the saved candidate without any worker memory.
    import subprocess
    saved_candidate_id = subprocess.check_output(
        [
            sys.executable, '-c',
            "from pathlib import Path; from core.chapter_drafts import read_chapter_rewrite; "
            "import sys; print(read_chapter_rewrite(Path(sys.argv[1]), 'offline_created').task_id)",
            str(staged.base_dir),
        ],
        text=True,
    ).strip()
    assert saved_candidate_id == candidate['task_id']
    assert candidate['title'] == '重写后的第二封信'
    rejected = client.post(url, json={'task_id': candidate['task_id'], 'expected_revision': '0' * 64})
    assert rejected.status_code == 409
    accepted = client.post(url, json={'task_id': candidate['task_id'], 'expected_revision': before_rewrite})
    assert accepted.status_code == 200, accepted.text
    assert client.get(url).json()['candidate'] is None
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

    # Imported drafts can be edited, extended manually and continued from
    # explicit author facts without reconstructing or rewriting old prose.
    imported = client.post('/api/novels/import_manuscript', json={
        'name': 'imported', 'title': '潮声稿', 'chapters': [
            {'title': '远灯', 'text': '她站在岸边。\\n\\n街灯还没有亮。'},
            {'title': '涨潮', 'text': '潮声从船底传来。'},
        ],
    })
    assert imported.status_code == 201, imported.text
    assert client.post('/api/novels/import_manuscript', json={
        'name': 'imported', 'title': '其他作品', 'chapters': [{'title': '序', 'text': '同名文件。'}],
    }).status_code == 409
    manager = StateManager('imported')
    assert manager.state.chapters[1].full_text == '她站在岸边。\\n\\n街灯还没有亮。'
    assert manager.state.imported_manuscript is True
    edited = client.patch('/api/novel/imported/chapter_text', json={
        'chapter': 1, 'text': '她站在雾中的岸边。', 'expected_revision': manager.revision,
    })
    assert edited.status_code == 200, edited.text
    manager = StateManager('imported')
    assert not manager.get_state_reconciliation_status()['required']
    appended = client.post('/api/novel/imported/chapters', json={
        'title': '回信', 'text': '她把问题留在回信中。', 'expected_revision': manager.revision,
    })
    assert appended.status_code == 201 and appended.json()['chapter'] == 3, appended.text
    manager = StateManager('imported')
    assert manager.state.chapters[3].full_text == '她把问题留在回信中。'
    facts = '信件只能在退潮后离开码头。'
    planned = client.post('/api/novel/imported/continuation_plan', json={
        'story_so_far': facts, 'direction': '找到失踪的船主', 'next_title': '访客',
        'next_scene': '一个认识船主的人抵达码头', 'chapters_to_plan': 4, 'expected_revision': manager.revision,
    })
    assert planned.status_code == 200, planned.text
    assert planned.json()['chapter'] == 4 and planned.json()['end'] == 7
    assert facts in StateManager('imported').build_chapter_context(4)['special_rules']
    continued_import = client.post('/api/novel/continue', json={
        'name': 'imported', 'count': 1, 'expected_revision': planned.json()['revision'],
        'include_adaptations': False,
    })
    assert continued_import.status_code == 200, continued_import.text
    assert client.get('/api/status').json()['current_step'] == '全部生成完成'
    imported_state = StateManager('imported')
    assert imported_state.state.chapters[1].full_text == '她站在雾中的岸边。'
    assert imported_state.state.chapters[4].title == '访客'
    assert imported_state.state.delivery_package is None
    assert not (imported_state.multimodal_dir / 'short_drama_chap_004.json').exists()

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
