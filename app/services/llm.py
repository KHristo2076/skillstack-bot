import logging
from openai import AsyncOpenAI
from app.config import settings

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=settings.openai_api_key)


class LLMClient:
    async def generate(
        self,
        system: str,
        user: str,
        max_tokens: int = 1000,
        model: str = "gpt-5.2",
    ) -> str:
        try:
            response = await client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_output_tokens=max_tokens,
            )

            return response.output_text

        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            raise


llm_client = LLMClient()