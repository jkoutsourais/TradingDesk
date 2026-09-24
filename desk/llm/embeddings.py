"""Ollama embeddings client (native /api/embed)."""

import httpx

from desk.config import EmbeddingModel


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str,
        config: EmbeddingModel,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(120.0, connect=5.0), transport=transport
        )

    @property
    def model(self) -> str:
        return self._config.model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([self._config.document_prefix + text for text in texts])

    async def embed_query(self, query: str) -> list[float]:
        """A search query; nomic-embed-text is trained with a separate query prefix."""
        return (await self._embed([self._config.query_prefix + query]))[0]

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        texts = inputs
        if not texts:
            return []
        body: dict[str, object] = {
            "model": self._config.model,
            "input": inputs,
            "keep_alive": self._config.keep_alive,
        }
        if self._config.cpu_only:
            body["options"] = {"num_gpu": 0}
        response = await self._http.post("/api/embed", json=body)
        response.raise_for_status()
        vectors: list[list[float]] = response.json().get("embeddings", [])
        if len(vectors) != len(texts):
            raise EmbeddingError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        wrong = {len(v) for v in vectors} - {self._config.dimensions}
        if wrong:
            raise EmbeddingError(
                f"{self._config.model} returned width {sorted(wrong)}, "
                f"expected {self._config.dimensions}"
            )
        return vectors

    async def aclose(self) -> None:
        await self._http.aclose()
