from typing import Literal, Optional, Union

import numpy as np
from rich.console import Console
from sentence_transformers import SentenceTransformer
from sklearn.base import ClusterMixin, TransformerMixin
from sklearn.cluster import OPTICS, AgglomerativeClustering
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.manifold import TSNE
from sklearn.preprocessing import label_binarize

from turftopic.base import ContextualModel, Encoder
from turftopic.feature_importance import (
    cluster_centroid_distance,
    soft_ctf_idf,
)
from turftopic.vectorizer import default_vectorizer

integer_message = """
You tried to pass an integer to ClusteringTopicModel as its first argument.
We assume you tried to specify the number of topics.
Since in ClusteringTopicModel the clustering model determines the number of topics,
and this process may be automatic, you have to pass along a clustering model
where the number of clusters is predefined.

For instance: ClusteringTopicModel(clustering=KMeans(10))

Alternatively you can reduce the number of topics in the model by specifying
the desired reduced number on initialization.

ClusteringTopicModel(n_reduce_to=10)
"""


def calculate_topic_vectors(
    cluster_labels: np.ndarray, embeddings: np.ndarray
) -> np.ndarray:
    """Calculates topic centroids."""
    centroids = []
    unique_labels = np.unique(cluster_labels)
    unique_labels = np.sort(unique_labels)
    for label in unique_labels:
        centroid = np.mean(embeddings[cluster_labels == label], axis=0)
        centroids.append(centroid)
    centroids = np.stack(centroids)
    return centroids


class ClusteringTopicModel(ContextualModel, ClusterMixin):
    """Topic models, which assume topics to be clusters of documents
    in semantic space.
    Models also include a dimensionality reduction step to aid clustering.

    ```python
    from turftopic import KeyNMF
    from sklearn.cluster import HDBSCAN
    import umap

    corpus: list[str] = ["some text", "more text", ...]

    # Construct a Top2Vec-like model
    model = ClusteringTopicModel(
        dimensionality_reduction=umap.UMAP(5),
        clustering=HDBSCAN(),
        feature_importance="centroid"
    ).fit(corpus)
    model.print_topics()
    ```

    Parameters
    ----------
    encoder: str or SentenceTransformer
        Model to encode documents/terms, all-MiniLM-L6-v2 is the default.
    vectorizer: CountVectorizer, default None
        Vectorizer used for term extraction.
        Can be used to prune or filter the vocabulary.
    dimensionality_reduction: TransformerMixin, default None
        Dimensionality reduction step to run before clustering.
        Defaults to TSNE with cosine distance.
        To imitate the behavior of BERTopic or Top2Vec you should use UMAP.
    clustering: ClusterMixin, default None
        Clustering method to use for finding topics.
        Defaults to OPTICS with 25 minimum cluster size.
        To imitate the behavior of BERTopic or Top2Vec you should use HDBSCAN.
    feature_importance: 'ctfidf' or 'centroid', default 'ctfidf'
        Method for estimating term importances.
        'centroid' uses distances from cluster centroid similarly
        to Top2Vec.
        'ctfidf' uses BERTopic's c-tf-idf.
    n_reduce_to: int, default None
        Number of topics to reduce topics to.
        The specified reduction method will be used to merge them.
        By default, topics are not merged.
    """

    def __init__(
        self,
        encoder: Union[
            Encoder, str
        ] = "sentence-transformers/all-MiniLM-L6-v2",
        vectorizer: Optional[CountVectorizer] = None,
        dimensionality_reduction: Optional[TransformerMixin] = None,
        clustering: Optional[ClusterMixin] = None,
        feature_importance: Literal["ctfidf", "centroid"] = "ctfidf",
        n_reduce_to: Optional[int] = None,
    ):
        self.encoder = encoder
        if isinstance(encoder, int):
            raise TypeError(integer_message)
        if isinstance(encoder, str):
            self.encoder_ = SentenceTransformer(encoder)
        else:
            self.encoder_ = encoder
        if vectorizer is None:
            self.vectorizer = default_vectorizer()
        else:
            self.vectorizer = vectorizer
        if clustering is None:
            self.clustering = OPTICS(min_samples=25)
        else:
            self.clustering = clustering
        if dimensionality_reduction is None:
            self.dimensionality_reduction = TSNE(
                n_components=2, metric="cosine"
            )
        else:
            self.dimensionality_reduction = dimensionality_reduction
        self.feature_importance = feature_importance
        self.n_reduce_to = n_reduce_to

    def _merge_agglomerative(self, n_reduce_to: int) -> dict[int, int]:
        n_topics = self.components_.shape[0]
        res = {old_label: old_label for old_label in self.classes_}
        if n_topics <= n_reduce_to:
            return res
        interesting_topic_vectors = np.stack(
            [
                vec
                for label, vec in zip(self.classes_, self.topic_vectors_)
                if label != -1
            ]
        )
        old_labels = [label for label in self.classes_ if label != -1]
        new_labels = AgglomerativeClustering(
            n_clusters=n_reduce_to, metric="cosine", linkage="average"
        ).fit_predict(interesting_topic_vectors)
        res = {}
        if -1 in self.classes_:
            res[-1] = -1
        for i_old, i_new in zip(old_labels, new_labels):
            res[i_old] = i_new
        return res

    def _estimate_parameters(
        self,
        cluster_labels: np.ndarray,
        embeddings: np.ndarray,
        doc_term_matrix: np.ndarray,
        status,
        console,
    ):
        clusters = np.unique(cluster_labels)
        self.classes_ = np.sort(clusters)
        self.topic_sizes_ = np.array(
            [np.sum(cluster_labels == label) for label in self.classes_]
        )
        status.update("Calculating topic vectors.")
        self.topic_vectors_ = calculate_topic_vectors(
            cluster_labels, embeddings
        )
        console.log("Topic vectors calculated")
        status.update("Encoding vocabulary")
        self.vocab_embeddings = self.encoder_.encode(self.vectorizer.get_feature_names_out())  # type: ignore
        console.log("Vocabulary encoded")
        status.update("Estimating term importances")
        if self.feature_importance == "ctfidf":
            document_topic_matrix = label_binarize(
                cluster_labels, classes=self.classes_
            )
            self.components_ = soft_ctf_idf(document_topic_matrix, doc_term_matrix)  # type: ignore
        else:
            self.components_ = cluster_centroid_distance(
                self.topic_vectors_,
                self.vocab_embeddings,
                metric="cosine",
            )
        self.labels_ = cluster_labels

    def fit_predict(
        self, raw_documents, y=None, embeddings: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """Fits model and predicts cluster labels for all given documents.

        Parameters
        ----------
        raw_documents: iterable of str
            Documents to fit the model on.
        y: None
            Ignored, exists for sklearn compatibility.
        embeddings: ndarray of shape (n_documents, n_dimensions), optional
            Precomputed document encodings.

        Returns
        -------
        ndarray of shape (n_documents)
            Cluster label for all documents (-1 for outliers)
        """
        console = Console()
        with console.status("Fitting model") as status:
            if embeddings is None:
                status.update("Encoding documents")
                embeddings = self.encoder_.encode(raw_documents)
                console.log("Encoding done.")
            status.update("Extracting terms")
            self.doc_term_matrix = self.vectorizer.fit_transform(raw_documents)
            console.log("Term extraction done.")
            status.update("Reducing Dimensionality")
            reduced_embeddings = self.dimensionality_reduction.fit_transform(
                embeddings
            )
            console.log("Dimensionality reduction done.")
            status.update("Clustering documents")
            cluster_labels = self.clustering.fit_predict(reduced_embeddings)
            print(np.unique(cluster_labels))
            console.log("Clustering done.")
            self._estimate_parameters(
                cluster_labels,
                embeddings,
                self.doc_term_matrix,
                status,
                console,
            )
            if self.n_reduce_to is not None:
                status.update("Reducing topics.")
                self.mapping_ = self._merge_agglomerative(self.n_reduce_to)
                cluster_labels = np.array(
                    [self.mapping_[label] for label in cluster_labels]
                )
                self._estimate_parameters(
                    np.array(cluster_labels),
                    embeddings,
                    self.doc_term_matrix,
                    status,
                    console,
                )
        console.log("Model fitting done.")
        return self.labels_

    def fit_transform(
        self, raw_documents, y=None, embeddings: Optional[np.ndarray] = None
    ):
        labels = self.fit_predict(raw_documents, y, embeddings)
        return label_binarize(labels, classes=self.classes_)
