"""
Moirai 示例脚本：在 Python 代码中直接调用全自动流水线
"""

from dotenv import load_dotenv

from pipeline.novel_pipeline import NovelPipeline

load_dotenv()

def main():
    # 1. 设定项目唯一标识名
    novel_project_name = "dead_replay_xianxia"

    # 2. 初始化全自动生产引擎
    pipeline = NovelPipeline(novel_id=novel_project_name)

    # 3. 传入一句话核心灵感
    idea_prompt = """
    主角穿越到一个残酷黑暗的修仙界底层，成为矿奴散修。
    激活金手指【生死推演回放】：只要触碰任何修士的尸体或遗物，就能在脑海中‘身临其境回放对方死前三天的记忆’，并从中强行窃取对方至死未曾传授的功法、藏宝地点或杀身大秘密！
    开局主角在废弃矿道发现了一具筑基期大修士的无名白骨，正在被矿场执事追杀逼问……
    """

    print("=" * 60)
    print("🚀 开始执行网文小说全自动生成流水线...")
    print("=" * 60)

    # 4. 执行全套策划与大纲构建
    concept = pipeline.initialize_new_novel(
        user_idea=idea_prompt,
        genre_preference="诡异修仙/反套路升级流",
        total_volumes=3,
        chapters_per_volume=10,
    )
    print(f"\n✔ 策划完成！书名：《{concept.title}》")
    print(f"核心卖点：{concept.logline}")

    # 5. 全自动连续生成黄金前 3 章（含微细纲、分节扩写、质检、去AI味润色、状态入库）
    print("\n✍️ 正在连续生成黄金前 3 章...")
    chapters = pipeline.generate_batch(start_chapter=1, count=3)

    print("\n" + "=" * 60)
    print(f"🎉 成功生成 {len(chapters)} 章正文！")
    print("=" * 60)

    # 6. 查看状态汇总
    summary = pipeline.get_status_summary()
    print(f"书名: 《{summary['title']}》")
    print(f"总字数: {summary['total_words']} 字")
    print(f"人物卡数量: {summary['character_count']}")
    print(f"已埋下伏笔数: {summary['foreshadowing_count']}")
    print(f"全本导出文件: {pipeline.state_manager.base_dir / f'{novel_project_name}_全本.md'}")


if __name__ == "__main__":
    main()
