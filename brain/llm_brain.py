import os
import re
import json
import datetime
import httpx
from openai import APITimeoutError, OpenAI
import config
from brain import memory_manager
from api import channel_router, feishu_api

# 🌟 引入我们刚刚写的圣遗物匹配模块
from skills.artifact_match import get_domain_by_user_intent
# 🌟 Boss 讨伐目标守卫：脚本按 `assets/Pathing/${name}前往.json` 拼路径文件名，
# 名字差一个字符（实测少打一个「·」）就会读不到文件、日志只留一句 JSON 解析失败；
# 并且 LLM 不知道角色对应哪只 Boss 时会按元素属性瞎猜（蓝砚被猜成无相之风），
# 所以这里用本地字典把「角色名 / 材料名」确定性翻译成官方 Boss 名
from skills.char_boss_match import resolve_boss_target
from skills.config_transaction import ConfigTransactionError
from skills.domain_match import describe_domain_resin_plan, resolve_domain_target
from skills.env_reader import ENV_CONTEXT_NAMES_KEY, env_context_age_note
from skills.route_group import plan_summary_lines, reclassify_free_tasks, redirect_run_boss_to_hunt

def load_system_prompt():
    """读取系统规则，优先 prompts/system_rules.md。"""
    fallback = "你是一个严谨的原神养成助手，回答要简洁、可执行。"
    try:
        with open(config.SYSTEM_RULES_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return content or fallback
    except Exception:
        return fallback


def build_model_messages(system_prompt, env_context, history_messages):
    """组装消息。

    🌟 展柜数据（env_context）必须紧贴最后一条用户消息，而不是排在历史之前：
    实测踩过的坑 —— 20 条滚动历史里有 9 条是模型自己说过的「蓝砚不在展柜 JSON 中」，
    而新鲜展柜排在它们前面，模型就近取用旧结论，明明展柜里有蓝砚也照旧回答"不在"。
    """
    history = list(history_messages or [])
    messages = [{"role": "system", "content": system_prompt}]
    if not history:
        messages.append({"role": "system", "content": env_context})
        return messages

    messages.extend(history[:-1])
    messages.append({"role": "system", "content": env_context})
    messages.append(history[-1])
    return messages


def _llm_timeout():
    """读超时（秒）。显式设置，避免用 SDK 默认的 600s 干等十几分钟才报错。"""
    try:
        value = float(os.getenv("LLM_TIMEOUT_SECONDS", "300"))
    except ValueError:
        value = 300.0
    return max(30.0, value)


def _llm_max_retries():
    try:
        return max(0, int(os.getenv("LLM_MAX_RETRIES", "2")))
    except ValueError:
        return 2


def _llm_stream_enabled():
    return os.getenv("LLM_STREAM", "1") != "0"


def _make_client():
    """
    根据 .env 配置文件选择对应的大模型 API 提供商，返回 OpenAI 兼容客户端。
    支持: github, openai, nvidia, custom, local

    🌟 显式给上 timeout —— 踩过的坑：不设时用 SDK 默认的 600s 读超时，
    推理型模型（如 deepseek-flash，会先产大量隐藏 reasoning token）+ 上万 token 的
    prompt，一次规划要等十几分钟，最后只丢一句 "Request timed out."。
    """
    timeout = httpx.Timeout(_llm_timeout(), connect=15.0)
    retries = _llm_max_retries()
    provider = os.getenv("LLM_PROVIDER", "github").lower()

    if provider == "github":
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("❌ GITHUB_TOKEN 未配置")
        return OpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=token,
            timeout=timeout,
            max_retries=retries,
        )

    elif provider in ("openai", "deepseek"):
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("❌ OPENAI_API_KEY 未配置")
        default_url = "https://api.deepseek.com/v1" if provider == "deepseek" else "https://api.openai.com/v1"
        base_url = os.getenv("OPENAI_BASE_URL", default_url)
        return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=retries)

    elif provider == "nvidia":
        api_key = os.getenv("NVIDIA_API_KEY", "")
        if not api_key:
            raise ValueError("❌ NVIDIA_API_KEY 未配置")
        base_url = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
        return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=retries)

    elif provider == "custom":
        api_key = os.getenv("CUSTOM_API_KEY", "")
        base_url = os.getenv("CUSTOM_BASE_URL", "")
        if not api_key or not base_url:
            raise ValueError("❌ CUSTOM_API_KEY 或 CUSTOM_BASE_URL 未配置")
        return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=retries)

    elif provider == "local":
        base_url = os.getenv("LOCAL_BASE_URL", "")
        if not base_url:
            raise ValueError("❌ LOCAL_BASE_URL 未配置。请先启动本地模型服务 (如 ollama、vLLM 等)")
        api_key = os.getenv("LOCAL_API_KEY", "local")
        print(f"🏠 正在连接本地模型服务: {base_url}")
        return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=retries)
    
    else:
        raise ValueError(f"❌ 不支持的 LLM_PROVIDER: {provider}，支持值: github, openai, nvidia, custom, local")


def complete_chat(client, model, messages, temperature=0.7):
    """调用大模型并返回文本。默认用**流式**。

    🌟 为什么必须流式：推理型模型（deepseek-flash 实测一句两字回复也花 63 个隐藏
    reasoning token）+ 上万 token 的规划 prompt，一次性等待可能十几分钟，
    撞上客户端读超时只丢一句 "Request timed out."。流式下每个 chunk 都会重置读计时，
    长回答不会被判超时；顺带还能边生成边打印进度。

    流式不支持时（部分中转/模型）自动退回一次性请求；已经在流式中超时则原样抛出，
    避免重复扣一次额度。
    """
    if _llm_stream_enabled():
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            parts = []
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if text:
                    parts.append(text)
            if parts:
                return "".join(parts)
            print("⚠️ 流式返回为空，改用一次性请求重试…")
        except APITimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - 中转兼容性差异很大，一律降级重试
            print(f"⚠️ 流式请求失败（{type(exc).__name__}: {exc}），改用一次性请求…")

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""


def llm_timeout_advice():
    """超时时给玩家的可操作建议。"""
    return (
        "❌ 请求超时：模型在超时时间内没有返回。\n"
        f"   当前读超时 = {_llm_timeout():.0f}s（.env 的 LLM_TIMEOUT_SECONDS 可调大）\n"
        "   可选做法：① 加大 LLM_TIMEOUT_SECONDS（如 600）；② 调小 MAX_HISTORY_MESSAGES（历史越长越慢）；\n"
        "   ③ 换更快的模型（推理型模型会先产大量隐藏 reasoning token，例如 deepseek-flash → deepseek-chat）；\n"
        "   ④ 直接重试一次（服务端偶发排队）。"
    )


APPROVAL_HEADER = "🛑 [系统拦截] 请确认是否执行上述计划？"
APPROVAL_HINTS = "👉 回复 'y' 批准执行\n👉 回复 't' 仅测试\n👉 直接回复其他内容进行反驳/修改"
CONCISE_HINTS = "👉 y = 执行　t = 仅改配置　其他内容 = 驳回并重新规划"


def build_approval_message(ai_reply, plan_text, guard_text="", concise=False):
    """组装审批消息。

    `concise=True`（QQ 这类手机聊天通道）时**不带大模型的推理正文**：
    实测 "推理正文 + ```json 计划块 + 审批屏" 一条就有 2400+ 字符，
    QQ 上会被切成 4 条消息、还把一行劈成两半，手机上根本没法看。
    精简版只留"要执行什么"，完整推理由调用方打到电脑端日志（信息不丢）。

    飞书 / 终端照旧发全文。
    """
    plan_block = f"\n{plan_text}{guard_text or ''}"
    if concise:
        return f"{APPROVAL_HEADER}{plan_block}\n\n{CONCISE_HINTS}"
    return f"{ai_reply}\n\n{'=' * 20}\n{APPROVAL_HEADER}{plan_block}\n{APPROVAL_HINTS}"


def ask_agent(messages, store, uid, open_id):
    """大模型思考、组装上下文、调用 OpenAI、解析 JSON 并发起审批或回复。"""
    try:
        system_prompt = load_system_prompt()

        # --- 日期偏移逻辑 ---
        business_time = datetime.datetime.now() - datetime.timedelta(hours=4)
        weekday_num = business_time.isoweekday()
        weekday_map = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
        today_str = f"星期{weekday_map[weekday_num]}"

        time_notice = (
            f"\n\n【系统实时时间注入】：今天是{today_str}（已对齐凌晨4点刷新）。"
            f"\n【展柜数据新鲜度】：{env_context_age_note(store)}。"
            "上面那段展柜是刚抓取的，优先级高于历史对话中任何关于展柜的旧说法；"
            "玩家点名的角色 / 武器 / 材料，必须先在展柜数据里查一遍再回答，"
            "严禁沿用你自己以前说过的「XX 不在展柜」——那可能早就过期了。"
            "请严格核对材料的 schedule，不包含今天的绝对不能排期！"
            "同时严禁提及任何不在展柜 JSON 数据中的角色 —— "
            "除非它出现在下面的【展柜外角色参考】里（那是从玩家账号直接读的）。"
        )

        # 玩家这一句话的原文（米游社补全、采集冷却"强制采集"判定都要用）
        latest_user_text = next(
            (
                str(item.get("content") or "")
                for item in reversed(messages or [])
                if item.get("role") == "user"
            ),
            "",
        )

        # 🌟 展柜外角色：只有玩家这句话里真的提到了、而且不在展柜里，才去问米游社
        #    （没配 cookie = 整体关闭；配了也是 1~3 天才拉一次名单，详情按需拉，避免风控）
        mys_notice = ""
        try:
            from skills import mys_api

            if mys_api.cookie_configured():
                mys_notice = mys_api.showcase_gap_notice(
                    latest_user_text, store.get(ENV_CONTEXT_NAMES_KEY) or []
                )
        except Exception as exc:                      # noqa: BLE001 —— 米游社挂了绝不能影响规划
            print(f"⚠️ 米游社参考数据未注入（不影响本轮）：{type(exc).__name__} {exc}")

        wallet = store.get("wallet", {"mora": 0, "exp_books": 0, "boss_mats": {}})
        boss_mats_str = "、".join([f"{k}: {v}个" for k, v in wallet.get("boss_mats", {}).items()]) or "无"

        wallet_notice = f"\n\n💰 【Agent 虚拟账本】当前已攒下：摩拉 {wallet['mora']}，经验书 {wallet['exp_books']} 本。\n📦 【已刷取Boss材料】：{boss_mats_str}\n（注：这仅代表系统近期的打工收益。规划前请严格对比材料缺口与已刷取数量，若已刷取数量 >= 缺口，必须停止安排该任务！）"

        # 🌟 采集物冷却（地区特产 48 小时刷新）：直接读 BetterGI 日志算出来的，只列"还没刷新"的。
        #    模型据此能认出"霜仙花还没刷新"，或者改推一个已经刷新的材料；角色名→采集物的对应
        #    由代码确定性解析（见 skills/gather_cooldown.resolve_material）。
        try:
            from skills import gather_cooldown

            cooldown_notice = gather_cooldown.cooldown_block()
            forced_gather = (
                config.GATHER_COOLDOWN_ALLOW_FORCE
                and gather_cooldown.is_forced(latest_user_text)
            )
        except Exception as exc:        # noqa: BLE001 —— 检查失败不该影响规划
            print(f"⚠️ 采集物冷却检查不可用（不影响本轮）：{type(exc).__name__} {exc}")
            cooldown_notice = ""
            forced_gather = False

        model_messages = build_model_messages(
            system_prompt=system_prompt,
            env_context=(
                store.get("env_context", "") + time_notice + wallet_notice + mys_notice + cooldown_notice
            ),
            history_messages=messages,
        )

        client = _make_client()
        model_name = os.getenv("MODEL_NAME", "gpt-4o-mini")
        ai_reply = complete_chat(client, model_name, model_messages)

        # persist assistant reply
        messages.append({"role": "assistant", "content": ai_reply})
        messages = memory_manager.trim_history(messages)
        store["messages"] = messages
        memory_manager.save_chat_store(store)

        # 查找 JSON 指令
        json_match = re.search(r'```json\n(.*?)\n```', ai_reply, re.DOTALL)
        if json_match:
            try:
                bgi_cmd = json.loads(json_match.group(1))

                # ==========================================
                # 🌟 系统操作（关闭原神 / 可选连 BetterGI 一起关）
                #    ⚠️ 这条路**不写任何 BetterGI 配置**，只出计划等玩家回 y，
                #    所以要在下面那堆"改配置"的拦截之前就分流掉。
                # ==========================================
                if isinstance(bgi_cmd, dict) and isinstance(bgi_cmd.get("system_task"), dict):
                    from skills import game_control

                    intent = game_control.normalize_intent(bgi_cmd["system_task"])
                    plan_text = "\n".join(game_control.plan_lines(intent))
                    concise = channel_router.wants_concise(open_id)
                    approval_msg = build_approval_message(ai_reply, plan_text, "", concise=concise)
                    feishu_api.send_feishu_msg(open_id, approval_msg)
                    store["pending_task"] = {
                        "system_task": intent,
                        "open_id": open_id,
                        "uid": uid,
                    }
                    memory_manager.save_chat_store(store)
                    print(f"🛑 已生成系统操作审批（{intent['action']}，等玩家回 y）")
                    return

                # ==========================================
                # 🌟 核心拦截层：圣遗物意图转化
                # ==========================================
                if isinstance(bgi_cmd, dict) and isinstance(bgi_cmd.get("energy_task"), dict) \
                        and bgi_cmd["energy_task"].get("action") == "run_artifact":
                    raw_target = bgi_cmd["energy_task"].get("target", "")
                    
                    # 动态读取原始 JSON 字典文件
                    # 动态读取原始 JSON 字典文件
                    raw_data_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory", "artifact_get_methods_raw.json")
                    real_domain = "未找到对应副本"
                    
                    try:
                        if os.path.exists(raw_data_path):
                            with open(raw_data_path, "r", encoding="utf-8") as f:
                                raw_json_data = json.load(f)
                            # 调用神器：转化为真实副本名
                            real_domain = get_domain_by_user_intent(raw_target, raw_json_data)
                        else:
                            print(f"⚠️ 找不到圣遗物原始字典: {raw_data_path}")
                    except Exception as e:
                        print(f"❌ 圣遗物映射发生错误: {e}")
                    
                    # 如果匹配成功，悄悄覆写 JSON 给外挂服用
                    if real_domain != "未找到对应副本":
                        print(f"🔄 圣遗物字典映射触发：将【{raw_target}】转化为副本【{real_domain}】")
                        bgi_cmd["energy_task"]["target"] = real_domain
                        bgi_cmd["energy_task"]["action"] = "run_domain" # 转化为物理外挂认识的指令
                    else:
                        print(f"⚠️ 无法映射圣遗物【{raw_target}】，将原样下发测试。")

                # 取出 energy_task 供后续检查与展示（键存在但值为 None 时也要兜底）
                energy_task = bgi_cmd.get("energy_task") if isinstance(bgi_cmd, dict) else None
                if not isinstance(energy_task, dict):
                    energy_task = {}

                # 🌟 拦截层产生的提示（类目改判 / Boss 翻译 / 秘境翻译…）统一收进这里，
                #    审批屏会把它们贴在计划下方。
                #    ⚠️ 必须在下面所有 append 之前初始化：以前这行写在 append 之后，
                #    只要模型写错类目（需要改判）就 UnboundLocalError —— 异常被外层 except 吞掉，
                #    审批屏根本不发、pending_task 也不落盘，整轮白跑。
                guard_notices = []

                # 🌟 自由任务类目改判：LLM 会把「久雨莲」这类食材/矿物当成特产写成 gather，
                # 于是去「地图素材」里找 → 0 条命中。这里按"哪一类里真的有路线"改判。
                free_tasks = bgi_cmd.get("free_task") if isinstance(bgi_cmd, dict) else None
                for reclassify_notice in reclassify_free_tasks(free_tasks):
                    guard_notices.append(reclassify_notice)

                # 🌟 采集物冷却（地区特产 48 小时刷新）：把状态摆到审批屏上，
                #    让玩家在点 y 之前就知道"这个材料其实还没刷新"。
                #    执行时还会再拦一道（skills/bgi_controller._filter_gather_cooldown）。
                gather_targets = [
                    str(item.get("target") or "")
                    for item in (free_tasks or [])
                    if isinstance(item, dict) and item.get("action") == "gather"
                ]
                if gather_targets:
                    if forced_gather:
                        # 玩家说了「强制采集」：明确告诉执行层别拦
                        bgi_cmd["force_gather"] = True
                    try:
                        from skills import gather_cooldown

                        cooldown_lines, _blocked = gather_cooldown.notice_lines(gather_targets)
                        guard_notices.extend(cooldown_lines)
                    except Exception as exc:        # noqa: BLE001 —— 检查失败不该挡住规划
                        print(f"⚠️ 采集物冷却检查失败（不影响审批）：{type(exc).__name__} {exc}")

                # 🌟 Boss 讨伐目标提前翻译 + 对齐：LLM 给角色名（「蓝砚」）就给官方 Boss 名，
                # 给错名字就纠正，解析不出来（不支持的 Boss / 字典缺口）就把问题摆到审批文本里，
                # 让玩家回一句就行 —— 不写配置、不启动。
                if energy_task.get("action") == "run_boss":
                    # 🌟 先把"敌人路线被误当成 Boss"的情况改判成 hunt
                    redirect_notice = redirect_run_boss_to_hunt(bgi_cmd)
                    if redirect_notice:
                        energy_task = bgi_cmd["energy_task"]
                        guard_notices.append(redirect_notice)
                    else:
                        try:
                            fixed_boss, boss_notices = resolve_boss_target(energy_task.get("target"))
                            energy_task["target"] = fixed_boss
                            bgi_cmd["energy_task"]["target"] = fixed_boss
                            # 用 extend 而不是整体替换：上面的类目改判提示不能被冲掉
                            guard_notices.extend(boss_notices)
                        except ConfigTransactionError as e:
                            guard_notices.append(str(e))

                # 🌟 秘境目标提前翻译：LLM 可能只给角色名（「去打蓝砚武器的突破副本」→
                # target="蓝砚"），代码自己看展柜取武器 → 查字典得到炼武秘境 + domain_index，
                # 免得把角色名原样写进 BetterGI 的 DomainName。
                if energy_task.get("action") == "run_domain":
                    try:
                        domain_result, domain_notices = resolve_domain_target(
                            energy_task.get("target"), uid=store.get("uid") or config.DEFAULT_UID
                        )
                        energy_task["target"] = domain_result.domain
                        bgi_cmd["energy_task"]["target"] = domain_result.domain
                        if domain_result.domain_index:
                            energy_task["domain_index"] = domain_result.domain_index
                            bgi_cmd["energy_task"]["domain_index"] = domain_result.domain_index
                        guard_notices.extend(domain_notices)
                        # 只刷 N 次的树脂策略：写盘在 bgi_controller，这里只在审批屏提示
                        guard_notices.append(
                            describe_domain_resin_plan(energy_task.get("count"))
                        )
                    except ConfigTransactionError as e:
                        guard_notices.append(str(e))

                # 生成发送给用户的审批文本 (把"本轮将执行"列全，含 script 整脚本任务)
                target_domain = energy_task.get("target", "无")
                guard_text = ("\n" + "\n".join(guard_notices)) if guard_notices else ""

                from skills.bgi_controller import _registered_task_names  # 局部导入避免循环

                plan_text = "\n".join(
                    plan_summary_lines(
                        energy_task,
                        bgi_cmd.get("free_task"),
                        registered_names=_registered_task_names(),
                    )
                )
                concise = channel_router.wants_concise(open_id)
                approval_msg = build_approval_message(ai_reply, plan_text, guard_text, concise=concise)
                if concise:
                    # 手机上看不了这么长：QQ 只收上面的精简版，完整推理打到电脑端日志里
                    print("🧠 规划全文（聊天通道只收精简版）：\n" + ai_reply)

                feishu_api.send_feishu_msg(open_id, approval_msg)

                store["pending_task"] = {
                    "bgi_cmd": bgi_cmd,
                    "open_id": open_id,
                    "uid": uid,
                }
                memory_manager.save_chat_store(store)
                
            except json.JSONDecodeError:
                feishu_api.send_feishu_msg(open_id, ai_reply + "\n(解析 JSON 失败)")
        else:
            feishu_api.send_feishu_msg(open_id, ai_reply)

    except APITimeoutError:
        # 超时不影响后续对话，把建议发回飞书
        feishu_api.send_feishu_msg(open_id, llm_timeout_advice())
    except Exception as e:
        feishu_api.send_feishu_msg(open_id, f"❌ 大脑出错: {str(e)}")
