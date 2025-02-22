from __future__ import annotations

import datetime
import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)
from uuid import uuid4

import numpy as np
import weaviate
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore

from .utils import maximal_marginal_relevance

if TYPE_CHECKING:
    import weaviate


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%b-%d %I:%M %p"
)
handler.setFormatter(formatter)

logger.addHandler(handler)


def _default_schema(index_name: str) -> Dict:
    return {
        "class": index_name,
        "properties": [
            {
                "name": "text",
                "dataType": ["text"],
            }
        ],
    }


def _default_score_normalizer(val: float) -> float:
    # prevent overflow
    # use 709 because that's the largest exponent that doesn't overflow
    # use -709 because that's the smallest exponent that doesn't underflow
    val = np.clip(val, -709, 709)
    return 1 - 1 / (1 + np.exp(val))


def _json_serializable(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


class WeaviateVectorStore(VectorStore):
    """`Weaviate` vector store.

    To use, you should have the ``weaviate-client`` python package installed.

    Example:
        .. code-block:: python

            import weaviate
            from langchain_community.vectorstores import Weaviate

            client = weaviate.Client(url=os.environ["WEAVIATE_URL"], ...)
            weaviate = Weaviate(client, index_name, text_key)

    """

    def __init__(
        self,
        client: weaviate.WeaviateClient,
        index_name: str,
        text_key: str,
        embedding: Optional[Embeddings] = None,
        attributes: Optional[List[str]] = None,
        relevance_score_fn: Optional[
            Callable[[float], float]
        ] = _default_score_normalizer,
        use_multi_tenancy: bool = False,
    ):
        """Initialize with Weaviate client."""

        if not isinstance(client, weaviate.WeaviateClient):
            raise ValueError(
                "client should be an instance of"
                f" weaviate.WeaviateClient, got {type(client)}"
            )
        self._client = client
        self._index_name = index_name or f"LangChain_{uuid4().hex}"
        self._embedding = embedding
        self._text_key = text_key
        self._query_attrs = [self._text_key]
        self.relevance_score_fn = relevance_score_fn
        if attributes is not None:
            self._query_attrs.extend(attributes)

        schema = _default_schema(self._index_name)
        schema["MultiTenancyConfig"] = {"enabled": use_multi_tenancy}

        # check whether the index already exists
        if not client.collections.exists(self._index_name):
            client.collections.create_from_dict(schema)

        # store collection for convenience
        # this does not actually send a request to weaviate
        self._collection = client.collections.get(self._index_name)

        # store this setting so we don't have to send a request to weaviate
        # every time we want to do a CRUD operation
        self._multi_tenancy_enabled = self._collection.config.get(
            simple=False
        ).multi_tenancy_config.enabled

    @property
    def embeddings(self) -> Optional[Embeddings]:
        return self._embedding

    def _select_relevance_score_fn(self) -> Callable[[float], float]:
        return (
            self.relevance_score_fn
            if self.relevance_score_fn
            else _default_score_normalizer
        )

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        tenant: Optional[str] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Upload texts with metadata (properties) to Weaviate."""
        from weaviate.util import get_valid_uuid

        if tenant and not self._does_tenant_exist(tenant):
            logger.info(
                f"Tenant {tenant} does not exist in index {self._index_name}. "
                "Creating tenant."
            )
            tenant_objs = [weaviate.classes.tenants.Tenant(name=tenant)]
            self._collection.tenants.create(tenants=tenant_objs)

        ids = []
        embeddings: Optional[List[List[float]]] = None
        if self._embedding:
            embeddings = self._embedding.embed_documents(texts)

        with self._client.batch.dynamic() as batch:
            for i, text in enumerate(texts):
                data_properties = {self._text_key: text}
                if metadatas is not None:
                    for key, val in metadatas[i].items():
                        data_properties[key] = _json_serializable(val)

                # Allow for ids (consistent w/ other methods)
                # # Or uuids (backwards compatible w/ existing arg)
                # If the UUID of one of the objects already exists
                # then the existing object will be replaced by the new object.
                _id = get_valid_uuid(uuid4())
                if "uuids" in kwargs:
                    _id = kwargs["uuids"][i]
                elif "ids" in kwargs:
                    _id = kwargs["ids"][i]

                batch.add_object(
                    collection=self._index_name,
                    properties=data_properties,
                    uuid=_id,
                    vector=embeddings[i] if embeddings else None,
                    tenant=tenant,
                )

                ids.append(_id)

        failed_objs = self._client.batch.failed_objects
        for obj in failed_objs:
            err_message = (
                f"Failed to add object: {obj.original_uuid}\nReason: {obj.message}"
            )

            logger.error(err_message)

        return ids

    def _perform_search(
        self,
        query: str,
        k: int,
        keyword_query: Optional[str] = None,
        return_score=False,
        search_method: Literal["hybrid", "near_vector", "near_text"] = "hybrid",
        tenant: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Union[Document, Tuple[Document, float]]]:
        """
        Perform a similarity search.

        Parameters:
        query (str): The query string to search for.
        keyword_query (str): The seperate query string to search for using bm25.
        k (int): The number of results to return.
        return_score (bool, optional): Whether to return the score along with the
          document. Defaults to False.
        search_method (Literal['hybrid', 'near_vector'], optional): The search method
          to use. Can be 'hybrid' or 'near_vector'. Defaults to 'hybrid'.
        tenant (Optional[str], optional): The tenant name. Defaults to None.
        **kwargs: Additional parameters to pass to the search method. These parameters
          will be directly passed to the underlying Weaviate client's search method.

        Returns:
        List[Union[Document, Tuple[Document, float]]]: A list of documents that match
          the query. If return_score is True, each document is returned as a tuple
          with the document and its score.

        Raises:
        ValueError: If _embedding is None or an invalid search method is provided.
        """
        if search_method != "near_text" and self._embedding is None:
            raise ValueError("_embedding cannot be None for similarity_search if search_method is not near_text")

        """if "return_metadata" in kwargs and "score" not in kwargs["return_metadata"]:
            kwargs["return_metadata"].append("score")
        else:
            kwargs["return_metadata"] = ["score"]"""

        if (
            "return_properties" in kwargs
            and self._text_key not in kwargs["return_properties"]
        ):
            kwargs["return_properties"].append(self._text_key)

        return_uuids = kwargs.pop("return_uuids", False)

        with self._tenant_context(tenant) as collection:
            try:
                if search_method == "hybrid":
                    if self._embedding != None:
                        embedding = self._embedding.embed_query(query)
                        if keyword_query != None:
                            query = keyword_query
                        result = collection.query.hybrid(
                            query=query,
                            vector=embedding,
                            limit=k,
                            **kwargs
                        )
                    else:
                        result = collection.query.hybrid(
                            query=query,
                            #vector=embedding,
                            limit=k,
                            **kwargs
                        )
                elif search_method == "near_vector":
                    result = collection.query.near_vector(limit=k, **kwargs)
                elif search_method == "near_text":
                    result = collection.query.near_text(query=query, limit=k, **kwargs)
                else:
                    raise ValueError(f"Invalid search method: {search_method}")
            except weaviate.exceptions.WeaviateQueryException as e:
                raise ValueError(f"Error during query: {e}")

        docs = []
        for obj in result.objects:
            text = obj.properties.pop(self._text_key)
            filtered_metadata = {
                k: v
                for k, v in obj.metadata.__dict__.items()
                if v is not None
            }
            merged_props = {
                **obj.properties,
                **filtered_metadata,
                **({"vector": obj.vector["default"]} if obj.vector else {}),
                **({"uuid": str(obj.uuid)} if return_uuids else {}),
            }
            doc = Document(page_content=text, metadata=merged_props)
            if not return_score:
                docs.append(doc)
            else:
                score = str(obj.metadata.score) + ": " + str(obj.metadata.explain_score)
                docs.append((doc, score))

        return docs

    def similarity_search(
        self, query: str, k: int = 4, keyword_query: str = None, **kwargs: Any
    ) -> List[Document]:
        """Return docs most similar to query.

        Args:
            query: Text to look up documents similar to using vector similarity.
            keyword_query: Text to look up documents similar to using bm25.
            k: Number of Documents to return. Defaults to 4.
            **kwargs: Additional keyword arguments will be passed to the `hybrid()`
                function of the weaviate client.

        Returns:
            List of Documents most similar to the query.
        """

        search_method = kwargs.pop("search_method", "hybrid")
        result = self._perform_search(query, k, keyword_query, search_method=search_method, **kwargs)
        return result

    def similarity_search_by_vector(
        self, embedding: List[float], k: int = 4, **kwargs: Any
    ) -> List[Document]:
        """Look up similar documents by embedding vector in Weaviate."""

        return self._perform_search(
            query=None,
            k=k,
            near_vector=embedding,
            search_method="near_vector",
            **kwargs,
        )

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.

        Returns:
            List of Documents selected by maximal marginal relevance.
        """
        if self._embedding is not None:
            embedding = self._embedding.embed_query(query)
        else:
            raise ValueError(
                "max_marginal_relevance_search requires a suitable Embeddings object"
            )

        return self.max_marginal_relevance_search_by_vector(
            embedding, k=k, fetch_k=fetch_k, lambda_mult=lambda_mult, **kwargs
        )

    def max_marginal_relevance_search_by_vector(
        self,
        embedding: List[float],
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance.

        Maximal marginal relevance optimizes for similarity to query AND diversity
        among selected documents.

        Args:
            embedding: Embedding to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch to pass to MMR algorithm.
            lambda_mult: Number between 0 and 1 that determines the degree
                        of diversity among the results with 0 corresponding
                        to maximum diversity and 1 to minimum diversity.
                        Defaults to 0.5.

        Returns:
            List of Documents selected by maximal marginal relevance.
        """

        results = self._perform_search(
            query=None,
            k=fetch_k,
            include_vector=True,
            near_vector=embedding,
            search_method="near_vector",
            **kwargs,
        )

        embeddings = [result.metadata["vector"] for result in results]
        mmr_selected = maximal_marginal_relevance(
            np.array(embedding), embeddings, k=k, lambda_mult=lambda_mult
        )

        docs = []

        for idx in mmr_selected:
            text = results[idx].page_content
            results[idx].metadata.pop("vector")
            docs.append(Document(page_content=text, metadata=results[idx].metadata))

        return docs

    def similarity_search_with_score(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        """
        Return list of documents most similar to the query
        text and cosine distance in float for each.
        Lower score represents more similarity.
        """

        results = self._perform_search(query, k, return_score=True, **kwargs)

        return results

    @classmethod
    def from_texts(
        cls,
        texts: List[str],
        embedding: Embeddings,
        client: weaviate.WeaviateClient = None,
        metadatas: Optional[List[dict]] = None,
        tenant: Optional[str] = None,
        *,
        index_name: Optional[str] = None,
        text_key: str = "text",
        relevance_score_fn: Optional[
            Callable[[float], float]
        ] = _default_score_normalizer,
        **kwargs: Any,
    ) -> WeaviateVectorStore:
        """Construct Weaviate wrapper from raw documents.

        This is a user-friendly interface that:
            1. Embeds documents.
            2. Creates a new index for the embeddings in the Weaviate instance.
            3. Adds the documents to the newly created Weaviate index.

        This is intended to be a quick way to get started.

        Args:
            texts: Texts to add to vector store.
            embedding: Text embedding model to use.
            client: weaviate.Client to use.
            metadatas: Metadata associated with each text.
            tenant: The tenant name. Defaults to None.
            index_name: Index name.
            text_key: Key to use for uploading/retrieving text to/from vectorstore.
            relevance_score_fn: Function for converting whatever distance function the
                vector store uses to a relevance score, which is a normalized similarity
                score (0 means dissimilar, 1 means similar).
            **kwargs: Additional named parameters to pass to ``Weaviate.__init__()``.

        Example:
            .. code-block:: python

                from langchain_community.embeddings import OpenAIEmbeddings
                from langchain_community.vectorstores import Weaviate

                embeddings = OpenAIEmbeddings()
                weaviate = Weaviate.from_texts(
                    texts,
                    embeddings,
                    client=client
                )
        """

        attributes = list(metadatas[0].keys()) if metadatas else None

        weaviate_vector_store = cls(
            client,
            index_name,
            text_key,
            embedding=embedding,
            attributes=attributes,
            relevance_score_fn=relevance_score_fn,
            use_multi_tenancy=tenant is not None,
        )

        weaviate_vector_store.add_texts(texts, metadatas, tenant=tenant, **kwargs)

        return weaviate_vector_store

    def delete(
        self,
        ids: Optional[List[str]] = None,
        tenant: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Delete by vector IDs.

        Args:
            ids: List of ids to delete.
            tenant: The tenant name. Defaults to None.
        """

        if ids is None:
            raise ValueError("No ids provided to delete.")

        id_filter = weaviate.classes.query.Filter.by_id().contains_any(ids)

        with self._tenant_context(tenant) as collection:
            collection.data.delete_many(where=id_filter)

    def _does_tenant_exist(self, tenant: str) -> bool:
        """Check if tenant exists in Weaviate."""
        assert (
            self._multi_tenancy_enabled
        ), "Cannot check for tenant existence when multi-tenancy is not enabled"
        tenants = self._collection.tenants.get()

        return tenant in tenants

    @contextmanager
    def _tenant_context(
        self, tenant: Optional[str] = None
    ) -> Generator[weaviate.collections.Collection, None, None]:
        """Context manager for handling tenants.

        Args:
            tenant: The tenant name. Defaults to None.
        """

        if tenant is not None and not self._multi_tenancy_enabled:
            raise ValueError(
                "Cannot use tenant context when multi-tenancy is not enabled"
            )

        if tenant is None and self._multi_tenancy_enabled:
            raise ValueError("Must use tenant context when multi-tenancy is enabled")

        try:
            yield self._collection.with_tenant(tenant)
        finally:
            pass
