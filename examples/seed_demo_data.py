"""
Moirai 演示数据种子脚本：离线生成一部完整样例小说（不调用任何 LLM），
用于预览/开发 Web UI 的全部标签页。运行：python examples/seed_demo_data.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.multimodal_generator import AudioDramaPackage, ShortDramaScene, ShortDramaScript, VoiceActorProfile
from core.narrative_brancher import BranchingSimulationResult, PlotBranchCandidate
from core.reader_simulator import BulletComment, DropOffRiskPoint, ReaderSimulationReport
from core.schemas import (
    AuditReport,
    ChapterContent,
    ChapterOutline,
    CharacterCard,
    CommercialDeliveryPackage,
    CommercialTarget,
    EmotionalPacingBeat,
    FactionSetting,
    ForeshadowingRecord,
    GlobalOutline,
    NovelConcept,
    PacingSchedule,
    PowerLevel,
    StateUpdateDelta,
    VolumeOutline,
    WorldSetting,
)
from core.state_manager import StateManager

NOVEL_ID = "demo_showcase"

CH1_TEXT = """矿道深处的硫磺味浓得像化不开的雾。

楚封把最后一镐凿进岩壁，掌心震得发麻。三天了，他挖出的矿石还不到定额的一半。

"小子，还差三十斤。"监工的鞭梢在泥地上拖出沙沙的声响，"今晚挖不完，明天的口粮就减半。"

楚封没有回头。他盯着岩壁裂缝里那截露出半寸的惨白——那不是矿脉，是骨头。

一具枯坐的骸骨，保持着临死前向前爬行的姿势。

监工的脚步声远去之后，楚封用镐尖小心地撬开岩层。骸骨的指骨上套着一枚暗沉的骨戒，袍角残片上依稀可辨一个"沈"字。

指尖触上骨戒的刹那，冰凉的洪流猛地灌进天灵。

——雨夜。焚毁的山门。一个青袍修士把玉简拍进徒弟怀里："带着它走，别回头！"随后是漫天血光，以及一句几乎被雷声碾碎的遗言：九转吞天诀，起手第三式，逆运周天……

三百个呼吸后，楚封睁开眼。他的识海里多了一部完整的功法，还有一段刻骨的、不属于他的恨意。

这就是他的金手指：触碰亡者遗物，回放死前三天的记忆，并取走其中一切。

"矿奴楚封，练气一层。"他低声念着自己的身份，把骨戒扣进掌心，"从今夜起，不一样了。"

头顶的岩层忽然簌簌落灰。

一队黑甲执事举着照魂灯踏进矿道，灯光扫过之处，藏在暗处的偷灵者一个个被照得现形。为首那人手里捏着一张画像——画像上的脸，和骸骨袍角那个"沈"字指向的人，有七分相似。

照魂灯的光晕，正一寸一寸逼近他藏身的岩缝。"""

CH2_TEXT = """灯光扫过来的瞬间，楚封把自己埋进了矿渣堆。

照魂灯的原理他很清楚——回放记忆时听得一清二楚：此灯照魂不照形，只对活人识海中的灵光起反应。而他体内那部九转吞天诀，此刻正处在"逆运周天"的第一层，灵光内敛如死水。

灯，从他头顶三尺掠了过去。

"奇怪，画像上的人就死在这片矿区。"为首的黑甲执事皱眉收灯，"撤。"

脚步声退去，楚封在矿渣里又躺了整整一炷香，才缓缓爬出来。

逆运周天的第一层，竟然还有匿息的效果。他默默把这条记在心里。

回到工棚，赵管事已经等在那里。胖子斜倚在门框上，手里的算盘拨得噼啪响："楚封，昨夜矿道里死了个黑甲执事，上头查得紧。你说——我该不该报个信，换二两银子？"

"管事想报就报。"楚封舀起一瓢冷水，"不过上个月张癞子告密，赏钱到手当晚就失足坠井了。管事是聪明人。"

赵管事脸上的肥肉抖了抖。半晌，他把算盘一收："算你狠。这个月的定额，减一成。"

夜里，楚封盘坐在草席上，开始正式修行九转吞天诀。

逆运的灵力像逆流而上的鱼，每冲过一处窍穴，就割开一道细密的痛楚。功法感应到他的坚持，识海深处忽然浮出一行血色小字：

【九转吞天诀·第一转：吞雷引气。需寻天雷淬体，否则经逆气冲，形神俱灭。】

窗外惊雷炸响，暴雨如注。

楚封望着天幕上翻涌的雷云，忽然笑了。这鬼地方什么都缺，就是不缺雷。

而矿场最深处那座废弃的引雷塔，塔尖正对着全矿区最高的雷巢。"""

CH3_TEXT = """三日后，雷暴如约而至。

楚封借着搬运矿渣的差事，摸到了引雷塔下。锈死的铁门被他一掌拍开——逆运周天淬炼过的肉身，力气已远超练气一层该有的水准。

塔顶雷巢里，紫电正在积聚。他按照记忆中沈前辈的路数，盘坐塔心，双手结出引雷印。

第一道天雷落下时，他差点当场昏死。

那种痛不是伤，而是把整个人拆开重新熔铸的痛。雷力顺着逆行的经脉横冲直撞，他的皮肤焦黑迸裂，又在吞天诀的吞吐间缓慢重生。

第二道。第三道。

皮肉在雷光里反复碳化又重生，塔内几乎血肉横飞。

到第七道天雷劈落时，异变陡生——塔底的阴影里，一个佝偻的身影猛然抬头，双目赤红如血。

"吞天诀……竟然有人修成了吞天诀！"那身影嘶声狂笑，"小杂役，你可知这功法的上一任主人，是怎么死的？"

血袍老者的气息轰然爆发，练气九层的威压像山一样砸下来。楚封浑身骨头咔咔作响，连一根手指都抬不起来。

"把功法交出来，老夫给你个痛快。"

楚封伏在地上，肩膀剧烈颤抖，像是被吓破了胆。只有他自己知道，他的右手正死死按着塔心那道裂纹——那是引雷塔的灵力总枢。

"老东西，"他抬起头，眼里没有半分惧意，"你猜，我为什么专挑雷夜来这儿？"

塔顶雷巢中，积蓄了整夜的第九道天雷，轰然而落。

那一瞬间，楚封把最后一丝灵力灌进总枢。整座引雷塔化作一只咆哮的雷牢，血袍老者的狂笑被雷鸣碾成惨嚎。

雷光散尽，塔心焦黑一片。老者只余下一枚烧熔的身份令牌——黑水宗，执法堂，供奉。

楚封捡起令牌的手，稳得没有一丝颤抖。

但他也很清楚，雷牢困杀练气九层，动静瞒不住。天亮之前，黑水宗的追杀令就会贴满整个矿区。

他看了一眼东北方向——那里是矿区唯一的出路：乱葬岭。传说中，连修士的照魂灯都不敢照进去的地方。"""


def main():
    sm = StateManager(novel_id=NOVEL_ID)

    concept = NovelConcept(
        title="生死推演录",
        genre="诡异修仙",
        logline="矿奴少年触碰亡者遗物即可回放死前三日记忆、窃取功法与隐秘，在黑水宗的追杀网中步步登天。",
        golden_finger="生死回放：触碰亡者遗物，身临其境回放死前三天的记忆，并窃取其中功法、藏宝与秘密。",
        selling_points=["极致信息差", "越级反杀", "诡异氛围"],
        core_conflict="底层矿奴 vs 黑水宗与诸天暗手",
        core_taboos=["严禁虐主"],
        commercial_target=CommercialTarget(
            target_platform="FANQIE",
            protagonist_persona="RUTHLESS_DECISIVE",
            avoided_tropes=["严禁虐主与长周期憋屈", "反派拒绝无脑降智送经验"],
        ),
    )
    sm.set_concept(concept)

    world = WorldSetting(
        world_name="幽州界·黑水矿区",
        geography_overview="黑水矿区横亘幽州南境三百里，矿脉之下埋着上古战场的残骸。矿区东北是终年浓雾的乱葬岭，西南隔着黑水河与黑水宗主城相望。",
        power_system=[
            PowerLevel(level_name="练气", description="引气入体，分一至九层，矿区杂役多为练气一至三层。", breakthrough_criteria="灵力灌满十二正经"),
            PowerLevel(level_name="筑基", description="灵力化液，可御器飞行，寿元翻倍至两百载。", breakthrough_criteria="筑基丹或天雷淬体"),
            PowerLevel(level_name="金丹", description="一颗金丹镇一域，黑水宗内门长老之流。", breakthrough_criteria="丹火淬魂，神识凝形"),
            PowerLevel(level_name="元婴", description="元婴不灭，夺舍重生，一州之地跺脚震三颤。", breakthrough_criteria="渡心魔劫"),
            PowerLevel(level_name="化神", description="神游太虚，掌控法则碎片，传说之境。", breakthrough_criteria="悟透一道法则"),
            PowerLevel(level_name="渡劫", description="半步登仙，全界不超过五人，皆为活着的神话。", breakthrough_criteria="九重天劫"),
        ],
        factions=[
            FactionSetting(name="黑水宗", alignment="敌对", description="盘踞黑水河上游的魔修大宗，以矿税与血祭控制整个矿区，执法堂令人闻风丧胆。", key_figures=["血袍供奉", "执法堂主"]),
            FactionSetting(name="矿工暗伙", alignment="友好", description="矿奴们自发的地下互助网络，传递消息、倒卖灵矿，是楚封最早的耳目。", key_figures=["张癞子"]),
            FactionSetting(name="乱葬岭隐修", alignment="暗藏祸心", description="蛰伏于乱葬岭的神秘势力，似乎在寻找千年前陨落的吞天诀传承。", key_figures=[]),
        ],
        special_rules=["照魂灯只对活人识海灵光起反应，逆运功法可匿息", "吞天诀每升一转需吞纳对应天物淬体，否则形神俱灭", "乱葬岭内一切神识探查手段尽数失效"],
        unique_lore="千年前吞天老人以一己之力吞灭九大宗门，最终被围杀于乱葬岭，其道统下落成谜。",
    )
    sm.set_world_setting(world)

    chars = [
        CharacterCard(id="char_mc", name="楚封", role="主角", personality_tags=["隐忍", "杀伐果断", "重诺"],
                      background="自幼被卖入黑水矿区的孤儿矿奴，父母死于矿难，实为沈前辈一脉最后传人。",
                      speaking_style="话少，句句带钉",
                      secrets_and_goals=["掌握生死回放", "查明沈家灭门真相"]),
        CharacterCard(id="char_villain", name="赵管事", role="反派", personality_tags=["贪婪", "欺软怕硬"],
                      background="矿区底层管事，替黑水宗压榨矿奴，克扣口粮放高利贷。", speaking_style="算盘腔，满嘴生意"),
        CharacterCard(id="char_mentor", name="沈青崖", role="导师", personality_tags=["残魂", "护短"],
                      background="三十年前被黑水宗围杀的筑基修士，残魂寄于骨戒，是吞天诀原主。",
                      speaking_style="苍老，偶有疯癫"),
        CharacterCard(id="char_ally", name="柳青瑶", role="重要配角", personality_tags=["机敏", "身负血仇"],
                      background="伪装成矿工的散修，实为被黑水宗灭门的药王谷遗孤。", speaking_style="清冷，一针见血"),
    ]
    sm.upsert_characters_bulk(chars)

    chapters_outline = [
        (1, "矿道白骨", "楚封挖矿时惊现神秘骸骨，触碰骨戒激活生死回放，获得吞天诀传承，卷入黑甲执事搜捕。", "骸骨遗产与黑甲执事的搜捕", "遗产与追杀同时到来", ["楚封", "赵管事"]),
        (2, "匿息过灯", "楚封借逆运周天匿息躲过照魂灯，与赵管事第一次正面交锋，夜修吞天诀遭遇雷劫指引。", "照魂灯下的匿息博弈", "功法需吞雷淬体", ["楚封", "赵管事"]),
        (3, "雷牢葬供", "雷夜楚封闯引雷塔吞雷淬体，血袍供奉现身夺法，楚封以塔灵总枢引九天雷牢反杀，身份令牌牵出黑水宗。", "练气三层 vs 练气九层的生死局", "反杀后的逃亡抉择", ["楚封"]),
        (4, "乱葬岭", "楚封连夜出逃乱葬岭，照魂灯尽数失效，却撞见隐修势力的影子。", "逃亡与未知领地", "岭中有岭", ["楚封"]),
        (5, "雾中同行", "乱葬岭中与柳青瑶狭路相逢，互相试探后达成临时同盟。", "同行者亦是陌生人", "各怀鬼胎的同盟", ["楚封", "柳青瑶"]),
        (6, "骨殿遗音", "二人循残魂指引发现吞天老人骨殿，沈青崖残魂首次显形讲述灭门真相。", "骨殿中的三方试探", "三十年前的真相", ["楚封", "沈青崖", "柳青瑶"]),
    ]
    vol1 = VolumeOutline(
        volume_number=1, volume_title="雷起矿渊",
        volume_goal="楚封以矿奴之身修成吞天诀第一转，挣脱矿区枷锁",
        main_antagonist="黑水宗",
        volume_climax="黑水宗倾巢围山，楚封借天雷大阵焚尽追兵，名动幽州",
        chapters=[
            ChapterOutline(chapter_number=n, volume_number=1, title=t, summary=s,
                           main_conflict=conf, expectation_hook=hook, involved_character_names=names)
            for (n, t, s, conf, hook, names) in chapters_outline
        ],
    )
    vol2 = VolumeOutline(
        volume_number=2, volume_title="宗门惊变",
        volume_goal="楚封潜入黑水宗腹地，揭开沈家灭门血案",
        main_antagonist="执法堂主",
        volume_climax="执法堂主真面目揭晓，黑水宗内乱，楚封趁乱吞其金丹之气",
        chapters=[
            ChapterOutline(chapter_number=7, volume_number=2, title="入宗为奴", summary="楚封假意投靠黑水宗外门",
                           main_conflict="身份随时可能暴露", expectation_hook="堂主亲召", involved_character_names=["楚封"]),
            ChapterOutline(chapter_number=8, volume_number=2, title="药园杀机", summary="药园险局中初会执法堂主",
                           main_conflict="试探与反试探", expectation_hook="一枚同款骨戒", involved_character_names=["楚封", "柳青瑶"]),
        ],
    )
    pacing = PacingSchedule(
        total_chapters_planned=8,
        pacing_beats=[
            EmotionalPacingBeat(chapter_number=1, tension_level=70, anticipation_level=65, dopamine_payoff_level=85, pacing_type="爽点爆发", target_emotion_summary="绝境得宝，金手指炸裂登场"),
            EmotionalPacingBeat(chapter_number=2, tension_level=75, anticipation_level=80, dopamine_payoff_level=60, pacing_type="蓄势铺垫", target_emotion_summary="匿息过灯的窒息感与功法新规则"),
            EmotionalPacingBeat(chapter_number=3, tension_level=90, anticipation_level=85, dopamine_payoff_level=95, pacing_type="绝地反击", target_emotion_summary="越两级雷牢反杀，全书第一大爽点"),
            EmotionalPacingBeat(chapter_number=4, tension_level=65, anticipation_level=90, dopamine_payoff_level=55, pacing_type="突发异变", target_emotion_summary="逃亡与未知地图的探索欲"),
            EmotionalPacingBeat(chapter_number=5, tension_level=60, anticipation_level=75, dopamine_payoff_level=70, pacing_type="蓄势铺垫", target_emotion_summary="同盟达成，信息差扩大"),
            EmotionalPacingBeat(chapter_number=6, tension_level=70, anticipation_level=95, dopamine_payoff_level=80, pacing_type="收获清点", target_emotion_summary="真相揭开，仇恨升级"),
        ],
        climax_milestone_chapters=[3, 8],
    )
    sm.set_global_outline(GlobalOutline(
        novel_title=concept.title, main_theme="底层蝼蚁以吞天之志逆焚诸天",
        total_volumes=2, final_climax="楚封于九重天劫之下吞尽天雷，与幕后黑手对决于诸天裂缝",
        volumes=[vol1, vol2], pacing_schedule=pacing,
    ))

    audits = [AuditReport(passed=True, consistency_score=9.2, pacing_score=9.0, logic_issues=[],
                          character_ooc_issues=[], cliffhanger_detected=True, critique="节奏凌厉，钩子扎实。", revision_directives=[])]

    def make_chap(num, title, text, hook):
        wc = len([c for c in text if '\u4e00' <= c <= '\u9fff'])
        return ChapterContent(chapter_number=num, volume_number=1, title=title, word_count=wc,
                              beats_raw_text=[f"节拍{i}" for i in range(1, 4)], full_text=text,
                              cliffhanger_ending=hook, audit_history=audits)

    deltas = [
        StateUpdateDelta(summary_of_events="楚封触碰骸骨骨戒激活生死回放，获得九转吞天诀；黑甲执事持画像搜捕。",
                         character_updates={"楚封": {"level": "练气一层", "location": "黑水矿区矿道", "skills_and_abilities": ["九转吞天诀"]}},
                         newly_introduced_items=["沈氏骨戒"], current_location="黑水矿区矿道",
                         new_foreshadowings=[ForeshadowingRecord(hook_id="hook_001", title="沈家灭门真相", description="骸骨记忆中的雨夜灭门与青袍修士遗言，指向三十年前血案。", planted_chapter=1)]),
        StateUpdateDelta(summary_of_events="楚封发现逆运周天可匿息躲过照魂灯；识海浮现血字：第一转需吞雷淬体。",
                         character_updates={"楚封": {"level": "练气一层", "location": "矿区工棚", "current_mood": "蛰伏蓄力"}},
                         current_location="矿区工棚",
                         new_foreshadowings=[ForeshadowingRecord(hook_id="hook_002", title="废弃引雷塔", description="塔尖正对雷巢，是吞雷淬体的唯一地点。", planted_chapter=2)]),
        StateUpdateDelta(summary_of_events="楚封雷夜闯引雷塔吞雷淬体突破练气三层，以塔灵总枢引九天雷牢反杀血袍供奉，缴获黑水宗身份令牌。",
                         character_updates={"楚封": {"level": "练气三层", "location": "引雷塔塔心", "skills_and_abilities": ["吞雷引气"], "hp_status": "轻度雷灼，已缓解", "current_mood": "冷静亢奋"}},
                         newly_introduced_items=["黑水宗供奉令牌"], current_location="引雷塔塔心",
                         resolved_foreshadowing_ids=["hook_002"],
                         new_foreshadowings=[ForeshadowingRecord(hook_id="hook_003", title="黑水宗的画像", description="执事手中画像与沈姓骸骨七分相似——宗门早就在找什么人。", planted_chapter=3)]),
    ]
    texts = [(1, "矿道白骨", CH1_TEXT, "照魂灯的光晕，正一寸一寸逼近他藏身的岩缝。"),
             (2, "匿息过灯", CH2_TEXT, "而矿场最深处那座废弃的引雷塔，塔尖正对着全矿区最高的雷巢。"),
             (3, "雷牢葬供", CH3_TEXT, "他看了一眼东北方向——那里是矿区唯一的出路：乱葬岭。")]
    for (num, title, text, hook), delta in zip(texts, deltas, strict=True):
        sm.save_chapter(make_chap(num, title, text, hook), delta)

    # 知识图谱补充剧情关系
    kg = sm.knowledge_graph
    kg.upsert_node("沈青崖", "CHARACTER", {"role": "残魂导师"}, chapter=1)
    kg.add_relation("楚封", "沈氏骨戒", "WIELDS", "第1章矿道获得", chapter=1)
    kg.add_relation("沈青崖", "沈氏骨戒", "HIDDEN_IN", "残魂寄居", chapter=1)
    kg.add_relation("赵管事", "黑水宗", "SERVES", "矿区眼线", chapter=2)
    kg.add_relation("血袍供奉", "黑水宗", "SERVES", "执法堂供奉", chapter=3)
    kg.add_relation("楚封", "血袍供奉", "KILLED", "第3章雷牢反杀", chapter=3)
    kg.upsert_node("血袍供奉的令牌", "ITEM", {"grade": "身份令牌"}, chapter=3)
    sm.save_state()

    # 衍生数据：分镜 / 演播 / 推演 / 宣发
    sm.save_short_drama_script(1, ShortDramaScript(
        title="矿道白骨·第一集", core_hook="矿奴挖出一具骸骨，指尖触上的瞬间——三百段死人记忆灌进脑子！",
        scenes=[
            ShortDramaScene(scene_number=1, shot_type="特写", visual_action="龟裂的手掌抚过岩壁，镐尖凿出火星", sound_and_bgm="[矿镐闷响·心跳渐强]", dialogue="监工（阴沉）：还差三十斤。"),
            ShortDramaScene(scene_number=2, shot_type="快速推拉", visual_action="岩缝里露出一截惨白指骨，骨戒泛着幽光", sound_and_bgm="[骨裂声·BGM骤停]", dialogue="楚封（低语）：这不是矿……是骨头。"),
            ShortDramaScene(scene_number=3, shot_type="俯拍", visual_action="指尖触戒，瞳孔骤缩，雨夜焚山的记忆洪流炸开", sound_and_bgm="[雷鸣·血流声·配乐炸裂]", dialogue="沈青崖（回声）：带着它走，别回头！"),
            ShortDramaScene(scene_number=4, shot_type="全景", visual_action="照魂灯光晕一寸寸逼近岩缝，楚封眼神冷下来", sound_and_bgm="[心跳骤停BGM]", dialogue="楚封（内心）：从今夜起，不一样了。"),
        ],
        ending_cliffhanger="灯光停在他藏身的岩缝前一寸——下一集，匿息过灯！",
    ))
    sm.save_short_drama_script(2, ShortDramaScript(
        title="匿息过灯·第二集", core_hook="全矿区都在抓他，他却躺在矿渣里数完了追兵的脚步。",
        scenes=[
            ShortDramaScene(scene_number=1, shot_type="特写", visual_action="矿渣堆缝隙中一只紧盯上方的眼睛", sound_and_bgm="[压低的呼吸声]", dialogue="楚封（内心）：灯照魂，不照形。"),
            ShortDramaScene(scene_number=2, shot_type="中景", visual_action="赵管事拨着算盘堵在工棚门口", sound_and_bgm="[算盘脆响·阴乐]", dialogue="赵管事：你说，我该不该报个信？"),
            ShortDramaScene(scene_number=3, shot_type="快速推拉", visual_action="楚封舀起冷水一饮而尽，眼神像刀", sound_and_bgm="[水声·死寂]", dialogue="楚封：上个月告密的人，失足坠井了。"),
            ShortDramaScene(scene_number=4, shot_type="俯拍", visual_action="识海浮出血色小字，窗外惊雷炸响", sound_and_bgm="[惊雷·BGM拔高]", dialogue="楚封（笑）：这地方什么都缺，就是不缺雷。"),
        ],
        ending_cliffhanger="吞雷淬体，形神俱灭或一步登天——第三集雷牢见！",
    ))
    sm.save_audio_drama_package(AudioDramaPackage(
        novel_title=concept.title,
        voice_cast=[
            VoiceActorProfile(character_name="楚封", voice_type="磁性冷峻青叔音", speech_rate="低沉偏慢，杀伐台词骤然提速", sample_dialogue_audio_cue="平淡中藏锋，反杀台词一字一顿"),
            VoiceActorProfile(character_name="赵管事", voice_type="油腻圆滑中年音", speech_rate="语速快，尾音上挑", sample_dialogue_audio_cue="市侩里裹着色厉内荏"),
            VoiceActorProfile(character_name="沈青崖", voice_type="苍老沙哑残魂音", speech_rate="缓慢，带混响", sample_dialogue_audio_cue="回声处理，疯癫与清醒交替"),
            VoiceActorProfile(character_name="旁白", voice_type="沉稳叙事男声", speech_rate="标准中速", sample_dialogue_audio_cue="冷峻白描风，段落间留白"),
        ],
        audio_production_notes="全片低频环境音打底（矿道风声/水滴），雷劫段落叠加次声波压迫感；记忆回放统一加雨夜混响与轻微磁带抖动。",
    ))
    sm.save_branch_simulation(3, BranchingSimulationResult(
        decision_point_description="血袍供奉现身引雷塔，练气九层威压锁死塔心，楚封形同俎上鱼肉。",
        candidates=[
            PlotBranchCandidate(branch_id="A", branch_title="雷霆一搏·塔灵总枢", narrative_strategy="赌上全部灵力启动引雷塔总枢，借第九道天雷化作雷牢困杀供奉，置之死地而后生。",
                                immediate_conflict="灵力透支与雷力反噬的双重生死线", dopamine_score=95, logic_consistency_score=88, longterm_potential_score=82, overall_score=90.0,
                                recommendation_reason="延续'吞雷'主题，爽点与设定自洽双高，且令牌掉落可自然衔接黑水宗主线。"),
            PlotBranchCandidate(branch_id="B", branch_title="残魂现身·借师之威", narrative_strategy="骨戒残魂沈青崖短暂显形，以三十年前布置的暗手逼退供奉，师徒相认。",
                                immediate_conflict="残魂只能支撑三个呼吸", dopamine_score=78, logic_consistency_score=92, longterm_potential_score=90, overall_score=84.0,
                                recommendation_reason="情感浓度高，但高潮让渡给导师，削弱主角主动性。"),
            PlotBranchCandidate(branch_id="C", branch_title="金蝉脱壳·弃塔遁岭", narrative_strategy="匿息潜入塔底暗河脱身，放弃淬体机会远走乱葬岭。",
                                immediate_conflict="供奉封锁四门的水泄之围", dopamine_score=65, logic_consistency_score=85, longterm_potential_score=75, overall_score=72.0,
                                recommendation_reason="稳妥但压抑，连续两章无爆点有弃书风险。"),
        ],
        selected_branch_id="A",
    ))
    sm.save_reader_simulation(1, ReaderSimulationReport(
        chapter_number=1, predicted_completion_rate=94.0, three_chapter_retention_score=91.0,
        bullet_comments=[
            BulletComment(paragraph_index=3, reader_name="十年老书虫_断剑", sentiment="POSITIVE", content="骸骨保持爬行姿势这个细节，氛围感直接拉满"),
            BulletComment(paragraph_index=8, reader_name="快节奏爽文狂魔_阿飞", sentiment="POSITIVE", content="金手指第一章就给规则说明，不墨迹，好评"),
            BulletComment(paragraph_index=10, reader_name="考据细节狂_墨竹", sentiment="NEUTRAL", content="照魂灯只照活人这个设定记下了，后面要是忘了来打脸"),
            BulletComment(paragraph_index=12, reader_name="情绪共鸣党_晚晴", sentiment="WARNING", content="开篇压迫感可以，但别虐主角太久，两章内必须看到反击"),
        ],
        drop_off_risks=[DropOffRiskPoint(paragraph_index=2, risk_level="LOW", reason="矿场背景交代略密", fix_suggestion="把定额数字揉进监工的鞭子动作里，少一句旁白")],
        overall_sentiment_summary="氛围与爽点兼得，金手指规则清晰，追读意愿极强；需警惕连续压抑。",
    ))
    sm.set_delivery_package(CommercialDeliveryPackage(
        novel_title=concept.title,
        tiktok_promo_script="""【黄金前3秒】
（黑屏，只有一声骨裂）
旁白：如果碰一下死人骨头，就能看到他死前三天的一切——你敢碰吗？

【分镜1·画面：矿道灯下惨白骸骨】
音效：滴水声+心跳
旁白：矿奴楚封挖到的不是矿，是一位被灭门修士的全部遗产。

【分镜2·画面：记忆洪流炸开的雨夜焚山】
音效：雷鸣炸裂
旁白：功法、藏宝、血仇，摸一下，全带走。

【分镜3·画面：照魂灯逼近岩缝，楚封眼神冷了】
音效：BGM骤停
旁白：可全宗的追兵，也顺着这具骸骨找来了。

【高潮断章·画面：第九道天雷劈落引雷塔】
音效：天雷咆哮
旁白：练气三层，反杀练气九层——这一雷，他等了整整三章！

（定格黑屏）字幕：生死推演录，全平台可搜。""",

        ai_cover_prompts={
            "主封面": "A lone miner in ragged robes touching an ancient skeleton ring, ghostly memory streams exploding from his eyes, thunderstorm above a dark mine shaft, cinematic dark fantasy, volumetric lightning, ultra detailed, 8k --ar 3:4",
            "角色立绘": "Young cultivator with cold sharp eyes, ragged miner robes with subtle bone-ring artifact on finger, purple lightning arcs, dark xianxia style, character sheet, front view, 8k",
            "横幅海报": "Epic wide shot of an ancient lightning tower being struck by nine dragons of thunder, a tiny figure standing at its heart, apocalyptic purple storm, dark fantasy cinematic, 8k",
        },
        marketing_blurbs=[
            "金手指最爽的一种：摸尸体就能偷功法！《生死推演录》开局即高能。",
            "不圣母不憋屈，矿奴楚封用死人的记忆，活成活人的神话。",
            "每一章都在反杀，每一雷都在升级——诡异修仙流天花板。",
        ],
    ))
    sm.set_delivery_package  # noqa: B018 (确认写入)

    sm.export_markdown()
    sm.export_platform_delivery_files()
    print(f"✔ 演示项目 {NOVEL_ID} 已生成: {sm.base_dir}")


if __name__ == "__main__":
    main()
