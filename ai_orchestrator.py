# --- START OF FILE ai_orchestrator.py ---
# AI-оркестратор LEX v2 — только отвечает на вопросы, не пишет и не исполняет код.

import logging
import random
import aiohttp
import asyncio
import config


class AIOrchestrator:

    @staticmethod
    def get_providers():
        return [
            {"id": "groq",       "url": "https://api.groq.com/openai/v1/chat/completions",          "model": "llama-3.3-70b-versatile"},
            {"id": "openrouter", "url": "https://openrouter.ai/api/v1/chat/completions",             "model": "google/gemini-2.0-flash-001"},
            {"id": "deepseek",   "url": "https://api.deepseek.com/chat/completions",                 "model": "deepseek-chat"},
            {"id": "together",   "url": "https://api.together.xyz/v1/chat/completions",              "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
            {"id": "github",     "url": "https://models.inference.ai.azure.com/chat/completions",    "model": "gpt-4o"},
            {"id": "mistral",    "url": "https://api.mistral.ai/v1/chat/completions",                "model": "mistral-large-latest"},
        ]

    @staticmethod
    async def resilient_llm_call(system_prompt: str, user_prompt: str) -> str:
        """Перебирает всех провайдеров и ключи, пока не получит ответ."""
        providers = AIOrchestrator.get_providers()
        random.shuffle(providers)

        async with aiohttp.ClientSession() as session:
            for prov in providers:
                keys = config.API_KEYS.get(prov["id"], [])
                if not keys:
                    continue
                random.shuffle(keys)

                for key in keys:
                    headers = {
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    }
                    payload = {
                        "model": prov["model"],
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_prompt},
                        ],
                        "temperature": 0.2,
                    }
                    try:
                        async with session.post(prov["url"], headers=headers, json=payload, timeout=20) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                return data["choices"][0]["message"]["content"]
                            else:
                                logging.warning(f"[LLM] Ошибка {resp.status} на {prov['id']}")
                    except Exception as e:
                        logging.warning(f"[LLM] Тайм-аут/сбой на {prov['id']}: {e}")
                        continue

        return "❌ Ошибка: Все API ключи и провайдеры недоступны."
