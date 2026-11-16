"""Extracción de características de imágenes para el módulo vectorial.

Dos representaciones de una misma imagen:

* **Clásica (BoVW)**: descriptores locales SIFT de 128 dimensiones, cuantizados
  contra un *codebook* de ``K`` palabras visuales entrenado con K-Means. La imagen
  queda como un histograma de palabras, que se escribe como texto
  (``"v17:3 v402 v9:2"``) y se indexa con el índice invertido (TF-IDF + coseno).
* **Moderna (embedding denso)**: salida de 512 dimensiones de la penúltima capa de
  una ResNet-18 preentrenada en ImageNet, reducida a 128 con PCA y normalizada
  (norma L2 = 1). Se guarda en una columna ``VECTOR(128)`` y se indexa con IVF.

Los modelos (``codebook`` y ``PCA``) se entrenan una sola vez con
``data/prepare_fashion.py`` y se guardan en ``<MINIDB_DATA_DIR>/models/``; aquí se
cargan de forma perezosa para vectorizar la imagen de una consulta. ``torch`` y
``opencv`` solo se importan cuando se usan (el motor no depende de ellos).
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

EMBEDDING_DIM = 128
RESNET_DIM = 512
SIFT_MAX_KEYPOINTS = 100
SIFT_UPSCALE = 4
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
CODEBOOK_FILE = "bovw_codebook.npz"
PCA_FILE = "pca_resnet18.npz"


class FeatureError(Exception):
    """No se pudo procesar la imagen o faltan los modelos entrenados."""


def load_image(data: bytes):
    """Decodifica una imagen (JPEG, PNG, ...) a RGB con Pillow."""
    from PIL import Image, UnidentifiedImageError

    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise FeatureError(f"no se pudo leer la imagen: {exc}") from None


# ---------------------------------------------------------------------- SIFT + BoVW
_sift: Any = None


def sift_descriptors(image) -> np.ndarray:
    """Descriptores SIFT (n × 128) de una imagen PIL; se amplía ×4 porque las fotos son de 60 × 80."""
    import cv2

    global _sift
    if _sift is None:
        _sift = cv2.SIFT_create(nfeatures=SIFT_MAX_KEYPOINTS)
    gray = np.asarray(image.convert("L"))
    h, w = gray.shape
    scale = SIFT_UPSCALE if max(h, w) < 200 else 1
    if scale != 1:
        gray = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    elif max(h, w) > 480:
        f = 480 / max(h, w)
        gray = cv2.resize(gray, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    _, desc = _sift.detectAndCompute(gray, None)
    if desc is None:
        return np.zeros((0, 128), dtype=np.float32)
    return desc[:SIFT_MAX_KEYPOINTS].astype(np.float32)


def quantize(descriptors: np.ndarray, codebook: np.ndarray) -> np.ndarray:
    """Palabra visual (índice del centroide más cercano) de cada descriptor."""
    if len(descriptors) == 0:
        return np.zeros(0, dtype=np.int64)
    c_sq = np.einsum("ij,ij->i", codebook, codebook)
    return np.argmin(c_sq - 2.0 * (descriptors @ codebook.T), axis=1)


def words_text(words: np.ndarray) -> str:
    """Histograma de palabras visuales como texto: ``"v3:2 v17 v402:5"``."""
    if len(words) == 0:
        return ""
    ids, counts = np.unique(words, return_counts=True)
    return " ".join(f"v{int(i)}:{int(c)}" if c > 1 else f"v{int(i)}" for i, c in zip(ids, counts))


# ---------------------------------------------------------------------- ResNet-18
_model: Any = None
_device: Any = None


def _resnet():
    global _model, _device
    if _model is None:
        import torch
        from torchvision.models import ResNet18_Weights, resnet18

        net = resnet18(weights=ResNet18_Weights.IMAGENET1K_V1)
        net.fc = torch.nn.Identity()  # salida del avgpool: 512 dimensiones
        net.eval()
        _device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        _model = net.to(_device)
    return _model, _device


def _tensor(image) -> np.ndarray:
    """Imagen PIL -> arreglo 3 × 224 × 224 normalizado (se rellena a cuadrado con blanco)."""
    from PIL import Image

    w, h = image.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(image, ((side - w) // 2, (side - h) // 2))
    arr = np.asarray(canvas.resize((224, 224), Image.BILINEAR), dtype=np.float32) / 255.0
    arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
    return arr.transpose(2, 0, 1).astype(np.float32)


def resnet_features(images: Sequence[Any]) -> np.ndarray:
    """Vectores de 512 dimensiones de un lote de imágenes PIL."""
    import torch

    model, device = _resnet()
    batch = torch.from_numpy(np.stack([_tensor(im) for im in images])).to(device)
    with torch.no_grad():
        out = model(batch)
    return out.cpu().numpy().astype(np.float32)


def fit_pca(features: np.ndarray, dim: int = EMBEDDING_DIM) -> dict[str, np.ndarray]:
    """PCA por SVD de la matriz centrada; devuelve media, componentes y varianza explicada."""
    mean = features.mean(axis=0)
    centered = features - mean
    cov = (centered.T @ centered) / max(1, len(features) - 1)
    eigvals, eigvecs = np.linalg.eigh(cov.astype(np.float64))
    order = np.argsort(eigvals)[::-1][:dim]
    return {
        "mean": mean.astype(np.float32),
        "components": eigvecs[:, order].T.astype(np.float32),
        "explained": np.array([eigvals[order].sum() / eigvals.sum()], dtype=np.float32),
    }


def project(features: np.ndarray, pca: dict[str, np.ndarray]) -> np.ndarray:
    """Proyección PCA + normalización L2 (así coseno y producto punto coinciden)."""
    out = (features - pca["mean"]) @ pca["components"].T
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return (out / np.where(norms > 0, norms, 1.0)).astype(np.float32)


# ---------------------------------------------------------------------- consulta
class FeatureExtractor:
    """Vectoriza la imagen de una consulta con los modelos entrenados sobre el catálogo."""

    def __init__(self, models_dir: str | Path) -> None:
        self.models_dir = Path(models_dir)
        self._codebook: np.ndarray | None = None
        self._pca: dict[str, np.ndarray] | None = None

    @property
    def available(self) -> bool:
        return (self.models_dir / CODEBOOK_FILE).is_file() and (self.models_dir / PCA_FILE).is_file()

    def _load(self) -> None:
        if not self.available:
            raise FeatureError(
                f"faltan los modelos en {self.models_dir} (ejecute: python data/prepare_fashion.py)"
            )
        if self._codebook is None:
            self._codebook = np.load(self.models_dir / CODEBOOK_FILE)["codebook"]
        if self._pca is None:
            with np.load(self.models_dir / PCA_FILE) as z:
                self._pca = {k: z[k] for k in z.files}

    def extract(self, data: bytes) -> dict[str, Any]:
        """Embedding (128 floats) y palabras visuales de una imagen, con sus tiempos de extracción."""
        self._load()
        assert self._codebook is not None and self._pca is not None
        image = load_image(data)
        t0 = time.perf_counter()
        embedding = project(resnet_features([image]), self._pca)[0]
        t1 = time.perf_counter()
        descriptors = sift_descriptors(image)
        words = words_text(quantize(descriptors, self._codebook))
        t2 = time.perf_counter()
        return {
            "embedding": [round(float(x), 6) for x in embedding],
            "words": words,
            "keypoints": int(len(descriptors)),
            "embedding_ms": round((t1 - t0) * 1000, 2),
            "bovw_ms": round((t2 - t1) * 1000, 2),
            "width": image.size[0],
            "height": image.size[1],
        }
