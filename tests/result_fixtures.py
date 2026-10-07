"""Schema-valid fake model results shared by cache and job tests."""

from copy import deepcopy


def emotion() -> dict:
    return {
        "self_emotion": "平静",
        "other_emotion": "快乐",
        "self_intensity": 5,
        "other_intensity": 6,
        "self_keywords": ["在吗"],
        "other_keywords": ["在的"],
        "overall_tone": "轻松愉快",
        "self_evidence": "「在吗」",
        "other_evidence": "「在的」",
        "month_vibe": "一问一答，平稳收尾",
        "turning_point": "",
        "confidence": "medium",
    }


def topics() -> dict:
    return {
        "month_title": "《测试月》",
        "topics": [{"name": "日常", "weight": 1.0, "keywords": ["在吗"], "one_liner": "一句问候开场"}],
        "summary": "本月主要交换日常问候。",
        "topic_shift_detected": False,
        "shift_description": "",
        "confidence": "medium",
    }


def relationship() -> dict:
    return {
        "initiator_tendency": "balanced",
        "initiator_ratio_self": 0.5,
        "interaction_style": "日常问候",
        "closeness_score": 5,
        "closeness_trend": "稳定",
        "self_role": "倾诉者",
        "other_role": "倾听者",
        "emotional_support_self_to_other": "medium",
        "emotional_support_other_to_self": "medium",
        "secret_language": "",
        "push_pull": "双方轮流开口",
        "relationship_summary": "关系节奏保持稳定",
        "confidence": "medium",
    }


def habits() -> dict:
    return {
        "personality_tags": ["简洁"],
        "common_phrases": ["在吗"],
        "emoji_style": "极少",
        "top_emojis": [],
        "sentence_length": "短句为主",
        "reply_speed": "适中",
        "topic_jumping": "专注一个话题",
        "unique_traits": ["常用短句确认状态"],
        "language_fingerprint": "短句为主，表达直接。",
        "typing_persona": "短句确认派",
        "signature_moment": "「在吗」",
        "confidence": "medium",
    }


def profile(name: str = "测试成员", *, group: bool = False) -> dict:
    result = {
        "name": name,
        "overall_impression": "谨慎的短句派",
        "one_line_bio": "先问在不在，再决定说多少",
        "personality_analysis": {
            "core_type": "理性简洁",
            "strengths": ["表达直接（原句：'在吗'）"],
            "weaknesses": ["信息量偏少（原句：'在吗'）"],
            "quirks": ["先确认对方在线"],
            "thinking_style": "务实解决型",
            "humor_style": "轻微吐槽",
            "social_tendency": "选择性互动",
        },
        "chat_style_analysis": {
            "opener": "直接抛问题",
            "responder": "简短回应",
            "signature_phrases": ["在吗"],
            "punctuation_style": "标点简洁",
            "emoji_usage": "文字为主",
            "topic_preference": ["日常问候"],
            "topic_avoid": [],
        },
        "emotional_pattern": {
            "frequency": "medium",
            "typical_state": "平静",
            "stress_response": "先确认情况",
            "support_style": "给出简短回应",
            "trigger_topics": [],
            "recovery_speed": "medium",
        },
        "intelligence_indicators": {
            "thinking_depth": "先确认信息再行动",
            "learning_style": "边问边确认",
            "language_richness": "medium",
            "logic_consistency": "high",
        },
        "relationship_dynamics": {
            "role_in_relationship": "负责把问题抛到桌面上",
            "initiation_pattern": "用短句开启话题",
            "response_to_conflict": "先确认事实",
            "vulnerability_level": "medium",
            "what_they_seek": ["回应感"],
        },
        "growth_observation": {
            "has_changed": False,
            "change_description": "",
            "possible_reasons": [],
        },
        "fun_facts": ["常用一句问候作为开场"],
        "scoring": {
            "expressiveness": "5",
            "emotional_richness": "5",
            "logical_ratio": "理性50%,感性50%",
            "social_energy": "5",
            "uniqueness": "5",
        },
        "counter_evidence": [],
        "confidence": "medium",
        "roast_note": "以上结论只基于测试样本。",
        "verdict": "短句先遣队，问完在吗再决定出场。",
    }
    if group:
        result["group_specific"] = {
            "group_role": "捧哏王",
            "reply_pattern": "主要回应固定搭子",
            "presence": "有人说话时出现",
        }
    return result


def group_dynamics() -> dict:
    return {
        "group_vibe": "几个人轮流接话",
        "core_members": [{"name": "测试成员", "role": "社交枢纽", "evidence": "被回复 2 次"}],
        "sub_groups": [],
        "power_structure": "没有固定主导者",
        "newcomer_or_outsider": "",
        "lurker_ratio": 0.0,
        "conflict_moments": [],
        "pace": "日常续命型",
        "self_role": "参与者",
        "confidence": "medium",
    }


def group_topics() -> dict:
    return {
        "month_title": "《群聊测试月》",
        "topics": [
            {
                "name": "日常",
                "weight": 1.0,
                "keywords": ["在吗"],
                "key_members": ["测试成员"],
                "one_liner": "先确认谁在线",
            }
        ],
        "summary": "群里主要交换日常问候。",
        "topic_shift_detected": False,
        "shift_description": "",
        "confidence": "medium",
    }


def group_emotion() -> dict:
    return {
        "group_emotion": "轻松",
        "group_intensity": 5,
        "group_evidence": "「在吗」",
        "member_emotions": [{"name": "测试成员", "emotion": "平静", "intensity": 5, "evidence": "「在吗」"}],
        "emotion_flow": "整月基本平稳",
        "turning_point": "",
        "atmosphere_maker": "",
        "atmosphere_killer": "",
        "confidence": "medium",
    }


def recap() -> dict:
    return {
        "overall": "这段关系保持稳定。",
        "arc": [{"period": "2025-01", "phase": "开场", "text": "双方保持联系。"}],
        "turning_points": [],
        "who_drives": "双方轮流推动节奏。",
        "unread_between_lines": ["联系仍然持续"],
        "closing": "故事还在继续。",
    }


def ask() -> dict:
    return {"answer": "材料显示双方保持联系。", "confidence": "medium", "evidence": ["2025-01"]}


def for_dimension(dimension: str) -> dict:
    factories = {
        "emotion": emotion,
        "topics": topics,
        "relationship": relationship,
        "habits": habits,
        "profile": profile,
        "group_dynamics": group_dynamics,
        "group_topics": group_topics,
        "group_emotion": group_emotion,
        "member_profiles": lambda: profile(group=True),
        "recap": recap,
        "ask": ask,
    }
    try:
        return deepcopy(factories[dimension]())
    except KeyError as exc:
        raise AssertionError(f"missing fixture for {dimension}") from exc
