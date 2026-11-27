"""Run the real UI with disposable projects/configuration and no model transport.

Usage: python scripts/serve_demo.py --port 8766
Only this process is patched; ordinary web_ui.py behavior is unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class OfflineSDK:
    """No network-capable SDK is constructed, even after configuration changes."""

    def close(self) -> None:
        pass


def _offline_chat(*_args, **_kwargs):
    from core.llm import LLMCallError

    raise LLMCallError("离线演示模式：模型调用已禁用，不会产生费用")


def _offline_state_delta(_agent, chapter_number, **_kwargs):
    """Deterministic browser fixture: exercise repair publication without a provider."""
    from core.schemas import StateUpdateDelta

    return StateUpdateDelta(
        summary_of_events=f"离线验收：第 {chapter_number} 章状态已提取",
        current_location=f"离线地点 {chapter_number}",
    )


def _offline_plan_novel(
    _agent,
    user_idea,
    genre_preference="",
    commercial_target=None,
    *,
    concept=None,
    world_setting=None,
    initial_characters=None,
    stage_callback=None,
):
    """Deterministic model double that still publishes every real checkpoint."""
    from agents.concept_planner import InitialNovelPackage
    from core.schemas import CharacterCard, CharacterState, NovelConcept, WorldSetting

    if concept is None:
        concept = NovelConcept(
            title="离线星港档案",
            genre=genre_preference or "离线科幻悬疑",
            logline=f"一封只会在停电时出现的星港来信，迫使守夜人追查真相：{user_idea[:80]}",
            golden_finger="旧式罗盘能够标记被人为抹除的航路，但每次只能保留一条新线索。",
            selling_points=["封闭星港", "航路解谜", "确定性离线验收"],
            core_conflict="在下一次星潮封港前找回失踪航路，并阻止控制塔永久删除证据。",
            commercial_target=commercial_target,
        )
        if stage_callback is not None:
            stage_callback("concept", concept.model_copy(deep=True))
    if world_setting is None:
        world_setting = WorldSetting(
            world_name="回声星港",
            geography_overview="环形港区由控制塔、旧泊位和潮汐隧道组成，所有出口受星潮影响。",
            special_rules=["星潮期间无线通信失效", "旧式纸质航图不受控制塔篡改"],
            unique_lore="失踪航路曾连接一座被从公共档案中删除的观测站。",
        )
        if stage_callback is not None:
            stage_callback("world", world_setting.model_copy(deep=True))
    if initial_characters is None:
        initial_characters = [
            CharacterCard(
                id="watcher_lin", name="林岚", role="主角", personality_tags=["冷静", "执着"],
                background="星港夜班守望员，曾亲历一次未被官方承认的航路事故。",
                initial_state=CharacterState(level="见习守望员", location="旧泊位", equipment_and_items=["旧式罗盘"]),
            ),
            CharacterCard(
                id="controller_qiao", name="乔序", role="初始反派", personality_tags=["克制", "多疑"],
                background="控制塔值班主管，坚持所有异常都只是设备故障。",
                initial_state=CharacterState(level="控制塔主管", location="中央控制塔", equipment_and_items=["主控钥匙"]),
            ),
            CharacterCard(
                id="engineer_su", name="苏澄", role="重要盟友", personality_tags=["敏锐", "务实"],
                background="维护潮汐隧道的工程师，保存着事故前最后一份纸质检修记录。",
                initial_state=CharacterState(level="资深工程师", location="潮汐隧道", equipment_and_items=["纸质检修册"]),
            ),
        ]
        if stage_callback is not None:
            stage_callback("cast", [item.model_copy(deep=True) for item in initial_characters])
    return InitialNovelPackage(
        concept=concept,
        world_setting=world_setting,
        initial_characters=initial_characters,
    )


def _offline_global_outline(
    _agent, concept, world_setting, characters, total_volumes=3, chapters_per_volume=10,
):
    """Return a complete, ordered outline that must pass production validators."""
    from core.schemas import (
        ChapterOutline,
        EmotionalPacingBeat,
        GlobalOutline,
        PacingSchedule,
        VolumeOutline,
    )

    total_chapters = total_volumes * chapters_per_volume
    volumes = []
    pacing_beats = []
    for volume_number in range(1, total_volumes + 1):
        start = (volume_number - 1) * chapters_per_volume + 1
        end = volume_number * chapters_per_volume
        chapters = []
        for chapter_number in range(start, end + 1):
            beat = EmotionalPacingBeat(
                chapter_number=chapter_number,
                tension_level=min(95, 45 + chapter_number * 3),
                anticipation_level=min(95, 55 + chapter_number * 2),
                dopamine_payoff_level=min(95, 60 + chapter_number * 2),
                pacing_type="线索逼近" if chapter_number < end else "卷末异变",
                target_emotion_summary="线索更清晰，但控制塔的封锁同步收紧。",
            )
            pacing_beats.append(beat)
            chapters.append(ChapterOutline(
                chapter_number=chapter_number,
                volume_number=volume_number,
                title=f"停电后的第 {chapter_number} 封信",
                summary="林岚沿旧式罗盘的指向核对星港档案，在封锁升级前取得一项可验证的新证据。",
                main_conflict="守夜人的现场证据与控制塔不断改写的官方记录正面冲突。",
                expectation_hook="罗盘指针越过港区边界，指向档案中不存在的观测站。",
                involved_character_names=[character.name for character in characters[:3]],
                pacing_beat=beat.model_copy(deep=True),
            ))
        volumes.append(VolumeOutline(
            volume_number=volume_number,
            volume_title=f"回声航路·卷 {volume_number}",
            volume_goal="确认失踪航路的真实坐标并保存不可篡改的证据。",
            main_antagonist="拒绝公开旧事故记录的中央控制塔",
            volume_climax="星潮抵达时，旧航图与实时导航显示出两套互相矛盾的星港。",
            chapter_range={"start": start, "end": end},
            chapters=chapters,
        ))
    return GlobalOutline(
        novel_title=concept.title,
        main_theme="记忆可以被篡改，但亲手保存的证据仍能让真相重新出现。",
        total_volumes=total_volumes,
        final_climax="林岚在全港断电的瞬间重新点亮被删除的观测站。",
        volumes=volumes,
        pacing_schedule=PacingSchedule(
            total_chapters_planned=total_chapters,
            pacing_beats=pacing_beats,
            climax_milestone_chapters=[volume.chapter_range.end for volume in volumes],
        ),
    )


def _offline_chapter_beats(agent, chapter_context):
    return agent.build_fallback_plan(chapter_context)


def _offline_write_beat(_agent, beat, chapter_title, chapter_number, **_kwargs):
    return "\n\n".join((
        f"旧泊位的应急灯依次熄灭。林岚握住罗盘，指针在第 {chapter_number} 章的记录页上停住。"
        f"她没有追逐噪声，而是先核对「{beat.scene_goal}」留下的每一处痕迹。",
        f"控制塔的广播要求所有人撤离。林岚把时间、方位和设备编号逐项写进纸质航图，"
        f"再按「{beat.action_and_dialogue_plan}」推进现场核验；苏澄随后给出的检修记录与她的判断完全吻合。",
        f"乔序切断最后一组照明时，罗盘却指向港墙之外。林岚收好证据，抬头看向黑暗中的旧观测窗："
        f"“《{chapter_title}》不是故障报告，这是有人没能寄出的求救信。”",
    ))


_HARBOR_CHAPTERS = (
    ("退信", """雾退到第三盏码头灯时，林岚才看清窗缝里那封信。远处的灯火还在水上晃，信纸却晒得温热，像刚从谁的贴身口袋里拿出来。

封口按着一枚蓝色邮戳。她用指腹摸了一遍，认出是旧港的钟楼。钟楼拆掉那年，她刚上小学。

收信人写着她母亲的名字。地址倒是没错，连门牌上褪色的半个“七”字都照着描了下来。

她没拆，先去问楼下卖鱼的阿婆。阿婆正用剪刀修一张破网，抬头瞥了一眼，说今天第一班渡船还没靠岸。

“是从屋里塞出去的吧？”阿婆说。

林岚回到楼上，沿窗框找。木刺向外翻，挂着一点毛线。她昨天晾在屋里的围巾少了一针。

信封里没有纸张摩擦的响声。她把它举向晨光，一把小钥匙的影子从母亲的名字上滑了过去。

抽屉最底下还有一串旧钥匙。母亲每次离港都会拿走其中一把，回来再挂上。最后一次，钩子空了十七年。

楼下响起短短一声船笛。她把外套披在睡衣外头，连鞋带都没来得及系紧。

等她跑到栈桥尽头，渡船已经离岸。留在木板上的只有一只空竹篮，篮底压着她家那把备用钥匙。"""),
    ("潮汐表背面", """那年冬天的潮汐表还钉在售票亭里。林岚把纸掀起来，背面是一行母亲的字：等水落下去，再看桥底。

她坐在长椅上等。雾里有橹声，一下近，一下远。卖票的人把窗口推开一条缝，递给她一杯热水。

“她也坐过这儿。”他说，低头继续数零钱。

水面慢慢退到桥墩下的白漆线。木板底下露出一只铁皮盒，粗麻绳已经泡成了和泥一样的颜色。

她试了信封里的钥匙。锁簧轻轻跳了一下。盒里一叠旧船票，从十七年前起，每张都是往返票，每张都只撕去了一半。"""),
)


_SAMPLE_BEFORE = "沈星将信标钥匙留在只有南薇能打开的玻璃匣里。"
_SAMPLE_AFTER = "沈星伸手一摸，信标钥匙一直揣在她自己的大衣口袋里。"


def _offline_audit_chapter(_agent, chapter_number, chapter_title, **kwargs):
    from core.schemas import AuditFinding, AuditReport

    target = kwargs.get("chapter_text", "")
    sources = (kwargs.get("chapter_context") or {}).get("audit_passages", [])
    findings = []
    if _SAMPLE_AFTER in target:
        for source in sources:
            if _SAMPLE_BEFORE in source["text"]:
                findings.append(AuditFinding(
                    issue="信标钥匙在两章里被描述为留在不同位置。请核对是否有取回钥匙的情节。",
                    source_chapter=source["chapter"], source_quote=_SAMPLE_BEFORE,
                    draft_quote=_SAMPLE_AFTER, certainty="contradiction", category="ownership",
                ))
                break
    return AuditReport(
        passed=not findings,
        consistency_score=5.1 if findings else 9.4,
        pacing_score=9.1,
        cliffhanger_detected=True,
        findings=findings,
        critique=f"离线样书复现了第 {chapter_number} 章《{chapter_title}》的审阅流程。",
    )


def _offline_structured(_client, messages, response_model, **kwargs):
    from core.manuscript_facts import ManuscriptFacts
    from core.schemas import ManuscriptFact

    if response_model is not ManuscriptFacts:
        return _offline_chat()
    facts = []
    for section in messages[0]["content"].split("【第 ")[1:]:
        heading, _, source = section.partition(" 章摘录】")
        quote = source.strip().split("\n", 1)[0][:100]
        if heading.isdigit() and len(quote) >= 4:
            facts.append(ManuscriptFact(
                kind="timeline", subject="演示摘录", fact=quote,
                source_chapter=int(heading), quote=quote,
            ))
    return ManuscriptFacts(facts=facts)


def _offline_polish_chapter(_agent, raw_chapter_text, **_kwargs):
    return raw_chapter_text


def _offline_audio_package(_generator, novel_concept, characters):
    from core.multimodal_generator import AudioDramaPackage, VoiceActorProfile

    return AudioDramaPackage(
        novel_title=novel_concept.title,
        voice_cast=[VoiceActorProfile(
            character_name=character.name,
            voice_type="清晰克制的青年声线",
            sample_dialogue_audio_cue="压低音量，保持现场核验时的专注感",
        ) for character in characters],
        audio_production_notes="仅用于离线流水线验收；以低频星港环境音区分场景。",
    )


def _offline_short_drama(_generator, novel_concept, chapter):
    from core.multimodal_generator import ShortDramaScene, ShortDramaScript

    return ShortDramaScript(
        title=f"{novel_concept.title}·{chapter.title}",
        core_hook="全港断电后，一枚旧罗盘指向不存在的观测站。",
        scenes=[ShortDramaScene(
            scene_number=1,
            shot_type="特写",
            visual_action="应急灯熄灭，罗盘指针逆向转动。",
            sound_and_bgm="[电流中断][低频心跳]",
            dialogue="林岚（低声）：航路没有消失。",
        )],
        ending_cliffhanger="港墙外亮起一扇档案中不存在的窗。",
    )


def _offline_reader_simulation(_simulator, novel_concept, chapter):
    from core.reader_simulator import BulletComment, DropOffRiskPoint, ReaderSimulationReport

    return ReaderSimulationReport(
        chapter_number=chapter.chapter_number,
        predicted_completion_rate=92.5,
        three_chapter_retention_score=89.0,
        bullet_comments=[BulletComment(
            paragraph_index=0,
            reader_name="离线验收读者",
            sentiment="POSITIVE",
            content=f"《{chapter.title}》的纸质线索很抓人。",
        )],
        drop_off_risks=[DropOffRiskPoint(
            paragraph_index=1,
            risk_level="LOW",
            reason="现场核验信息稍密集。",
            fix_suggestion="保留关键设备编号，其余移入动作描写。",
        )],
        overall_sentiment_summary=f"离线读者认为《{novel_concept.title}》第 {chapter.chapter_number} 章值得追更。",
    )


def _offline_branch_simulation(
    _brancher, chapter_number, novel_concept, world_setting, characters, current_situation,
):
    from core.narrative_brancher import BranchingSimulationResult, PlotBranchCandidate

    protagonist = characters[0].name if characters else "守夜人"
    location = world_setting.world_name if world_setting else "港口"
    return BranchingSimulationResult(
        decision_point_description=f"{protagonist}在{location}核验第 {chapter_number} 章线索：{current_situation}",
        candidates=[
            PlotBranchCandidate(
                branch_id="A", branch_title="封锁旧观测站",
                narrative_strategy=f"沿《{novel_concept.title}》留下的线索封锁入口，公开原始记录。",
                immediate_conflict="控制塔要求立刻销毁记录。",
                recommendation_reason="证据链清晰，也给下一章留下追查的空间。",
            ),
            PlotBranchCandidate(
                branch_id="B", branch_title="借出一份假记录",
                narrative_strategy="把两份略有差别的抄本交给不同证人，从回信措辞判断谁在泄密。",
                immediate_conflict="一名证人认出了抄本上的假签名。",
                recommendation_reason="能从人物关系展开调查，但会消耗证人的信任。",
            ),
            PlotBranchCandidate(
                branch_id="C", branch_title="追随最后一班夜渡",
                narrative_strategy="放弃在岸边等候，带着记录登船，去找当晚唯一的目击者。",
                immediate_conflict="回港前，保管记录的箱子被调换。",
                recommendation_reason="可以引出新的地点，代价是暂时失去现有证据。",
            ),
        ],
        selected_branch_id="A",
    )


def _offline_delivery_package(_agent, concept, world_setting, protagonist, first_three_chapters):
    from core.schemas import CommercialDeliveryPackage

    chapter_titles = "、".join(chapter.title for chapter in first_three_chapters) or "尚未生成正文"
    return CommercialDeliveryPackage(
        novel_title=concept.title,
        tiktok_promo_script=(
            "【黄金三秒】全港断电后，一枚旧罗盘指向不存在的观测站。\n"
            f"【正文切片】{protagonist.name}沿着{world_setting.world_name}的纸质航图追查：{chapter_titles}。\n"
            "【断章】港墙外的窗突然亮了。"
        ),
        ai_cover_prompts={
            "主封面": "cinematic ring-shaped spaceport in blackout, antique compass, volumetric blue light",
            "角色立绘": f"{protagonist.name}, night watch uniform, antique compass, restrained sci-fi mystery",
        },
        marketing_blurbs=["断电星港里，只有纸质航图不会说谎。", "不存在的航路，寄来了真实的求救信。"],
    )


def _star_route_outline():
    from core.schemas import ChapterOutline, GlobalOutline, VolumeChapterRange, VolumeOutline

    opening = ChapterOutline(
        chapter_number=1, volume_number=1, title="玻璃匣",
        summary="停电前，南薇发现匣底的编号与失踪航线一致。",
        main_conflict="玻璃匣即将被收回，南薇必须决定是否打开它。",
        expectation_hook="旧航线上还有一盏亮着的灯。",
        involved_character_names=["沈星", "南薇"],
    )
    beacon = ChapterOutline(
        chapter_number=3, volume_number=1, title="雾中信标",
        summary="沈星追着雾里的灯号出港，南薇留在岸上校对最后一段坐标。",
        main_conflict="两份星图把同一座信标标在不同位置。",
        expectation_hook="灯号中夹着一段失踪船员的呼叫。",
        involved_character_names=["沈星", "南薇"],
    )
    return GlobalOutline(
        novel_title="群星航路", main_theme="沈星和南薇试图把一条从星图上消失的航路找回来。",
        total_volumes=1, final_climax="两人在雾散之前重新点亮信标，让失航的船找到归路。",
        volumes=[VolumeOutline(
            volume_number=1, volume_title="失落的航标", volume_goal="查清玻璃匣底编号的来历。",
            main_antagonist="被反复改写的港口档案", volume_climax="两人凭旧星图和灯号交叉确认信标的位置。",
            chapter_range=VolumeChapterRange(start=1, end=3), chapters=[opening, beacon],
        )],
    )


@contextmanager
def demo_environment(directory: Path):
    """Isolate every writable root; call in a dedicated server/test process."""
    from config.settings import Settings, get_settings
    from core.llm import LLMClient, reset_llm_client

    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Demo mode requires an empty disposable directory")

    environment = {name: str(field.default) for name, field in Settings.model_fields.items()}
    environment.update({
        "OUTPUT_DIR": str(directory / "projects"),
        "OPENAI_API_KEY": "offline-demo-not-a-real-key",
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        **{name: "offline-demo" for name in (
            "DEFAULT_MODEL", "WRITER_MODEL", "PLANNER_MODEL", "CRITIC_MODEL", "STATE_MODEL",
        )},
    })
    reset_llm_client()
    with (
        patch.dict(os.environ, environment),
        patch.dict(Settings.model_config, env_file=str(directory / ".env")),
        patch.object(LLMClient, "_new_client", return_value=OfflineSDK()),
        patch.multiple(LLMClient, _chat=_offline_chat, generate_structured=_offline_structured),
    ):
        get_settings.cache_clear()
        import web_ui
        from agents.beat_director import BeatDirectorAgent
        from agents.commercial_delivery import CommercialDeliveryAgent
        from agents.concept_planner import ConceptPlannerAgent
        from agents.consistency_auditor import ConsistencyAuditorAgent
        from agents.outline_architect import OutlineArchitectAgent
        from agents.scene_writer import SceneWriterAgent
        from agents.state_updater import StateUpdaterAgent
        from agents.stylist_polisher import StylistPolisherAgent
        from core.multimodal_generator import MultimodalGenerator
        from core.narrative_brancher import NarrativeBrancher
        from core.reader_simulator import ReaderSimulator
        from core.schemas import ChapterContent, CharacterCard, CharacterState, ForeshadowingRecord, NovelConcept
        from core.state_manager import StateManager

        # Assets/HTML are already bound to the source tree. Only the config write
        # destination is redirected here; settings reads use the same temp file.
        with (
            patch.multiple(web_ui, BASE_DIR=directory, OFFLINE_DEMO=True),
            patch.object(ConceptPlannerAgent, "plan_novel", _offline_plan_novel),
            patch.object(OutlineArchitectAgent, "generate_global_outline", _offline_global_outline),
            patch.object(BeatDirectorAgent, "plan_chapter_beats", _offline_chapter_beats),
            patch.object(SceneWriterAgent, "write_beat", _offline_write_beat),
            patch.object(ConsistencyAuditorAgent, "audit_chapter", _offline_audit_chapter),
            patch.object(StylistPolisherAgent, "polish_chapter", _offline_polish_chapter),
            patch.object(StateUpdaterAgent, "extract_state_delta", _offline_state_delta),
            patch.object(MultimodalGenerator, "generate_audio_drama_package", _offline_audio_package),
            patch.object(MultimodalGenerator, "generate_short_drama_script", _offline_short_drama),
            patch.object(ReaderSimulator, "simulate_chapter_readers", _offline_reader_simulation),
            patch.object(NarrativeBrancher, "simulate_branches", _offline_branch_simulation),
            patch.object(CommercialDeliveryAgent, "generate_delivery_package", _offline_delivery_package),
        ):
            try:
                for novel_id, title, logline in (
                    ("demo_alpha", "雾港来信", "林岚在窗缝里捡到一封寄给母亲的信，邮戳上的钟楼在她上小学那年就拆了。"),
                    ("demo_beta", "群星航路", "星图上的旧航线早已消失，沈星和南薇却在一只玻璃匣底找到了它的编号。"),
                ):
                    manager = StateManager(novel_id)
                    manager.set_concept(NovelConcept(
                        title=title, genre="离线演示", logline=logline,
                        golden_finger="演示数据，不调用模型。", core_conflict="所有修改仅保存到临时目录。",
                    ))
                    manager.upsert_character(CharacterCard(
                        id=f"{novel_id}_hero", name="林岚" if novel_id == "demo_alpha" else "沈星",
                        role="主角",
                        background=(
                            "她认出了信封上的钟楼，那座楼在她上小学那年就拆了。"
                            if novel_id == "demo_alpha" else "天文台停电前，沈星请南薇校对最后一份星图。"
                        ),
                        initial_state=CharacterState(
                            level="凡人", location="启程地", hp_status="健康",
                            equipment_and_items=["旧信笺"], current_mood="警觉",
                        ),
                        current_state=CharacterState(location="码头") if novel_id == "demo_beta" else CharacterState(),
                    ))
                    if novel_id == "demo_beta":
                        manager.upsert_character(CharacterCard(
                            id="demo_beta_nanwei", name="南薇", role="星图校对员",
                            background="她是唯一能打开玻璃匣的人。",
                            initial_state=CharacterState(location="天文台"),
                            current_state=CharacterState(location="天文台", current_mood="专注"),
                        ))
                        graph = manager.knowledge_graph
                        for name, kind in (("天文台", "LOCATION"), ("星图", "ITEM"), ("玻璃匣", "ITEM"),
                                           ("信标钥匙", "ITEM"), ("旧航线之谜", "SECRET")):
                            graph.upsert_node(name, kind)
                        for source, relation, target in (
                            ("沈星", "委托", "南薇"), ("沈星", "到访", "天文台"),
                            ("南薇", "校对", "星图"), ("南薇", "能打开", "玻璃匣"),
                            ("信标钥匙", "封存于", "玻璃匣"), ("玻璃匣", "编号指向", "旧航线之谜"),
                        ):
                            graph.add_relation(source, target, relation)
                        manager.save_state()
                        manager.set_global_outline(_star_route_outline())
                        manager.create_snapshot(0)
                    for number in (1, 2):
                        if novel_id == "demo_alpha":
                            chapter_title, body = _HARBOR_CHAPTERS[number - 1]
                        else:
                            chapter_title = "玻璃匣" if number == 1 else "码头夜潮"
                            body = (
                                "天文台停电前，沈星请南薇检查最后一份星图。\n\n"
                                + _SAMPLE_BEFORE + "\n\n匣底的编号与消失的旧航线一致。"
                                if number == 1 else
                                "夜潮升起，两个人在码头分开。\n\n"
                                + _SAMPLE_AFTER + "\n\n信标就在雾中。"
                            )
                        manager.save_chapter(ChapterContent(
                            chapter_number=number, volume_number=1, title=chapter_title,
                            full_text=body, word_count=len(body), beats_raw_text=[], cliffhanger_ending="",
                        ))
                        if novel_id == "demo_beta":
                            if number == 1:
                                manager.upsert_foreshadowing(ForeshadowingRecord(
                                    hook_id="route-number", title="旧航线之谜", status="DEVELOPING",
                                    description="玻璃匣底的编号，与港口已经撤下的旧航线一致。",
                                    planted_chapter=1, expected_resolution_volume=1,
                                ))
                            manager.create_snapshot(number)
                yield web_ui.app
            finally:
                reset_llm_client()
                get_settings.cache_clear()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn

    with (
        tempfile.TemporaryDirectory(prefix="novelengine-offline-demo-") as directory,
        demo_environment(Path(directory)) as app,
    ):
        print(f"Offline disposable demo: {directory}", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
