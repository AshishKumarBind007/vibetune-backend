
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException
)

from fastapi.middleware.cors import (
    CORSMiddleware
)

from pydantic import (
    BaseModel,
    Field
)

from recommender import (
    load_recommendation_data,
    build_engine,
    recommend_songs_v3,
    normalize_genre_tag,
    MOOD_MAP,
    ENERGY_TARGETS,
)


DATASET_PATH = (
    "vibetune_emotion_dataset.parquet"
)


# ============================================================
# GLOBAL ENGINE STATE
# ============================================================

engine = {
    "rec_df": None,
    "genre_index": None,
    "emotion_matrix": None,
    "diversity_matrix": None,
}


# ============================================================
# STARTUP
# ============================================================

@asynccontextmanager
async def lifespan(app):

    print(
        "VibeTune: loading recommendation dataset..."
    )

    rec_df = load_recommendation_data(
        DATASET_PATH
    )

    (
        rec_df,
        genre_index,
        emotion_matrix,
        diversity_matrix,
    ) = build_engine(
        rec_df
    )

    engine["rec_df"] = rec_df
    engine["genre_index"] = genre_index
    engine["emotion_matrix"] = emotion_matrix
    engine["diversity_matrix"] = diversity_matrix

    print(
        "VibeTune: recommendation engine ready."
    )

    yield

    engine["rec_df"] = None
    engine["genre_index"] = None
    engine["emotion_matrix"] = None
    engine["diversity_matrix"] = None

    print(
        "VibeTune: engine released."
    )


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="VibeTune API",
    description=(
        "AI music recommendation backend"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# REQUEST MODEL
# ============================================================

class RecommendationRequest(
    BaseModel
):

    mood: str = Field(
        ...,
        description="Desired mood"
    )

    genre: Optional[str] = Field(
        default=None,
        description="Optional genre"
    )

    energy: str = Field(
        default="medium",
        description="Energy level"
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description=(
            "Number of recommendations"
        )
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "name": "VibeTune API",
        "version": "1.0.0",
        "status": "running",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    if engine["rec_df"] is None:

        return {
            "status": "starting"
        }

    return {

        "status": "healthy",

        "dataset_rows": len(
            engine["rec_df"]
        ),

        "genres": len(
            engine["genre_index"]
        ),

        "moods": len(
            MOOD_MAP
        ),

        "energies": list(
            ENERGY_TARGETS.keys()
        ),
    }


# ============================================================
# MOODS
# ============================================================

@app.get("/moods")
def moods():

    return {
        "moods": sorted(
            MOOD_MAP.keys()
        )
    }


# ============================================================
# ENERGIES
# ============================================================

@app.get("/energies")
def energies():

    return {
        "energies": sorted(
            ENERGY_TARGETS.keys()
        )
    }


# ============================================================
# GENRES
# ============================================================

@app.get("/genres")
def genres():

    return {

        "count": len(
            engine["genre_index"]
        ),

        "genres": sorted(
            engine["genre_index"].keys()
        )
    }


# ============================================================
# RECOMMEND
# ============================================================

@app.post("/recommend")
def recommend(
    request: RecommendationRequest
):

    if engine["rec_df"] is None:

        raise HTTPException(
            status_code=503,
            detail=(
                "Recommendation engine "
                "is still loading."
            )
        )

    mood = (
        request.mood
        .strip()
        .lower()
    )

    energy = (
        request.energy
        .strip()
        .lower()
    )

    if mood not in MOOD_MAP:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid mood "
                f"'{request.mood}'. "
                f"Supported moods: "
                f"{sorted(MOOD_MAP.keys())}"
            )
        )

    if energy not in ENERGY_TARGETS:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid energy "
                f"'{request.energy}'. "
                f"Supported energies: "
                f"{sorted(ENERGY_TARGETS.keys())}"
            )
        )

    genre = None

    if request.genre is not None:

        genre = normalize_genre_tag(
            request.genre
        )

        if genre not in (
            engine["genre_index"]
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid genre "
                    f"'{request.genre}'."
                )
            )

    try:

        recommendations = (
            recommend_songs_v3(
                rec_df=engine[
                    "rec_df"
                ],

                genre_index_v3=engine[
                    "genre_index"
                ],

                emotion_matrix=engine[
                    "emotion_matrix"
                ],

                diversity_matrix=engine[
                    "diversity_matrix"
                ],

                mood=mood,

                genre=genre,

                energy=energy,

                n=request.limit,
            )
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc)
        )

    return {

        "query": {
            "mood": mood,
            "genre": genre,
            "energy": energy,
            "limit": request.limit,
        },

        "count": len(
            recommendations
        ),

        "recommendations":
            recommendations,
    }
