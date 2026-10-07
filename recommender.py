
import re
import numpy as np
import pandas as pd

from sklearn.metrics.pairwise import cosine_similarity


# ============================================================
# DATASET
# ============================================================

DATASET_PATH = "vibetune_emotion_dataset.parquet"

RECOMMENDATION_COLUMNS = [
    "ISRC",
    "Artist(s)",
    "song",
    "Album",
    "Genre",
    "contexts",
    "Energy",
    "Danceability",
    "Positiveness",
    "Tempo",
    "Loudness (db)",
    "Speechiness",
    "Liveness",
    "Acousticness",
    "Instrumentalness",
    "Popularity",
    "emotion_joy",
    "emotion_sadness",
    "emotion_anger",
    "emotion_fear",
    "emotion_love",
    "emotion_surprise",
    "emotion_predicted"
]


EMOTION_FEATURES = [
    "emotion_joy",
    "emotion_sadness",
    "emotion_anger",
    "emotion_fear",
    "emotion_love",
    "emotion_surprise"
]


DIVERSITY_FEATURES = [
    "Energy",
    "Danceability",
    "Positiveness",
    "Tempo",
    "Acousticness",
    "Instrumentalness"
]


# ============================================================
# FROZEN MOOD MAP
# ============================================================

MOOD_MAP = {

    "happy": {
        "emotion_joy": 1.0,
        "emotion_love": 0.25,
        "emotion_surprise": 0.20
    },

    "sad": {
        "emotion_sadness": 1.0,
        "emotion_love": 0.20,
        "emotion_fear": 0.10
    },

    "angry": {
        "emotion_anger": 1.0,
        "emotion_energy": 0.80
    },

    "romantic": {
        "emotion_love": 1.0,
        "emotion_joy": 0.30
    },

    "fearful": {
        "emotion_fear": 1.0,
        "emotion_sadness": 0.20
    },

    "surprised": {
        "emotion_surprise": 1.0,
        "emotion_joy": 0.30
    },

    "calm": {
        "emotion_sadness": 0.15,
        "emotion_love": 0.35
    }
}


ENERGY_TARGETS = {
    "low": 0.25,
    "medium": 0.55,
    "high": 0.85
}


# ============================================================
# DATA LOADING
# ============================================================

def load_recommendation_data(dataset_path=DATASET_PATH):

    df = pd.read_parquet(
        dataset_path,
        columns=RECOMMENDATION_COLUMNS
    )

    # Preserve the final Phase-6 dataset.
    # Only the single no-lyrics row is excluded.
    df = df[
        df["emotion_predicted"].notna()
    ].reset_index(drop=True)

    numeric_columns = [
        "Energy",
        "Danceability",
        "Positiveness",
        "Tempo",
        "Loudness (db)",
        "Speechiness",
        "Liveness",
        "Acousticness",
        "Instrumentalness",
        "Popularity",
    ] + EMOTION_FEATURES

    for col in numeric_columns:

        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

        df[col] = df[col].fillna(
            df[col].median()
        )

    return df


# ============================================================
# GENRE NORMALIZATION
# ============================================================

def normalize_genre_tag(tag):

    tag = str(tag).strip().lower()

    tag = tag.replace("-", " ")
    tag = tag.replace("_", " ")

    tag = re.sub(
        r"\s+",
        " ",
        tag
    )

    replacements = {
        "hiphop": "hip hop",
        "hip hop": "hip hop",
        "r&b": "rnb",
        "rnb": "rnb",
    }

    return replacements.get(
        tag,
        tag
    )


def extract_genre_tags(value):

    if pd.isna(value):
        return []

    parts = re.split(
        r"[,;/|]+",
        str(value).lower()
    )

    tags = []

    for part in parts:

        tag = normalize_genre_tag(part)

        if tag:
            tags.append(tag)

    return list(
        dict.fromkeys(tags)
    )


# ============================================================
# ENGINE INDEX BUILDING
# ============================================================

def build_engine(df):

    df = df.copy()

    df["Genre_clean"] = df[
        "Genre"
    ].apply(
        extract_genre_tags
    )

    genre_index = {}

    for idx, genres in enumerate(
        df["Genre_clean"]
    ):

        for genre in genres:

            if genre not in genre_index:
                genre_index[genre] = []

            genre_index[genre].append(
                idx
            )

    for genre in genre_index:

        genre_index[genre] = np.asarray(
            genre_index[genre],
            dtype=np.int32
        )

    popularity = pd.to_numeric(
        df["Popularity"],
        errors="coerce"
    ).fillna(0)

    if popularity.max() > popularity.min():

        df["Popularity_normalized"] = (
            (
                popularity
                - popularity.min()
            )
            /
            (
                popularity.max()
                - popularity.min()
            )
        )

    else:

        df["Popularity_normalized"] = 0.0

    emotion_matrix = df[
        EMOTION_FEATURES
    ].values.astype(
        np.float32
    )

    diversity_matrix = df[
        DIVERSITY_FEATURES
    ].values.astype(
        np.float32
    )

    return (
        df,
        genre_index,
        emotion_matrix,
        diversity_matrix
    )


# ============================================================
# V3 RECOMMENDATION ENGINE
# ============================================================

def recommend_songs_v3(
    rec_df,
    genre_index_v3,
    emotion_matrix,
    diversity_matrix,
    mood,
    genre=None,
    energy="medium",
    n=10,
    artist_penalty=0.12
):

    mood = mood.lower().strip()
    energy = energy.lower().strip()

    if mood not in MOOD_MAP:

        raise ValueError(
            f"Unsupported mood: {mood}"
        )

    if energy not in ENERGY_TARGETS:

        raise ValueError(
            f"Unsupported energy: {energy}"
        )

    # --------------------------------------------------------
    # Candidate pool
    # --------------------------------------------------------

    if genre is not None:

        genre = normalize_genre_tag(
            genre
        )

        if genre not in genre_index_v3:

            raise ValueError(
                f"Genre '{genre}' not found."
            )

        candidates = genre_index_v3[
            genre
        ].copy()

    else:

        candidates = np.arange(
            len(rec_df),
            dtype=np.int32
        )

    if len(candidates) == 0:

        raise ValueError(
            "No recommendation candidates available."
        )

    # --------------------------------------------------------
    # Mood target
    # --------------------------------------------------------

    target_emotion = np.zeros(
        len(EMOTION_FEATURES),
        dtype=np.float32
    )

    for emotion, weight in MOOD_MAP[
        mood
    ].items():

        if emotion in EMOTION_FEATURES:

            idx = EMOTION_FEATURES.index(
                emotion
            )

            target_emotion[idx] = weight

    # --------------------------------------------------------
    # Emotion similarity
    # --------------------------------------------------------

    candidate_emotions = emotion_matrix[
        candidates
    ]

    emotion_similarity = cosine_similarity(
        candidate_emotions,
        target_emotion.reshape(
            1,
            -1
        )
    ).ravel()

    # --------------------------------------------------------
    # Energy similarity
    # --------------------------------------------------------

    target_energy = ENERGY_TARGETS[
        energy
    ]

    candidate_energy = rec_df.loc[
        candidates,
        "Energy"
    ].values.astype(
        np.float32
    )

    energy_similarity = (
        1.0
        - np.abs(
            candidate_energy
            - target_energy
        )
    )

    energy_similarity = np.clip(
        energy_similarity,
        0.0,
        1.0
    )

    # --------------------------------------------------------
    # Positiveness
    # --------------------------------------------------------

    positivity = rec_df.loc[
        candidates,
        "Positiveness"
    ].values.astype(
        np.float32
    )

    positivity = np.clip(
        positivity,
        0.0,
        1.0
    )

    # --------------------------------------------------------
    # Popularity
    # --------------------------------------------------------

    popularity_score = rec_df.loc[
        candidates,
        "Popularity_normalized"
    ].values.astype(
        np.float32
    )

    # --------------------------------------------------------
    # Genre relevance
    # --------------------------------------------------------

    if genre is not None:

        genre_score = np.ones(
            len(candidates),
            dtype=np.float32
        )

    else:

        genre_score = np.zeros(
            len(candidates),
            dtype=np.float32
        )

    # --------------------------------------------------------
    # Frozen V3 relevance weights
    # --------------------------------------------------------

    base_score = (

        0.42 * emotion_similarity

        + 0.23 * energy_similarity

        + 0.10 * positivity

        + 0.10 * popularity_score

        + 0.15 * genre_score
    )

    # --------------------------------------------------------
    # MMR diversity
    # --------------------------------------------------------

    candidate_diversity = diversity_matrix[
        candidates
    ]

    selected = []

    remaining = list(
        range(
            len(candidates)
        )
    )

    while (
        remaining
        and len(selected) < n
    ):

        if not selected:

            best_position = remaining[
                np.argmax(
                    base_score[
                        remaining
                    ]
                )
            ]

        else:

            selected_vectors = (
                candidate_diversity[
                    selected
                ]
            )

            remaining_vectors = (
                candidate_diversity[
                    remaining
                ]
            )

            similarity_matrix = (
                cosine_similarity(
                    remaining_vectors,
                    selected_vectors
                )
            )

            max_similarity = (
                similarity_matrix.max(
                    axis=1
                )
            )

            mmr_scores = (

                0.80
                * base_score[
                    remaining
                ]

                - 0.20
                * max_similarity
            )

            best_position = remaining[
                np.argmax(
                    mmr_scores
                )
            ]

        selected.append(
            best_position
        )

        remaining.remove(
            best_position
        )

    # --------------------------------------------------------
    # Artist diversity
    # --------------------------------------------------------

    final_positions = []

    used_artists = set()

    for position in selected:

        idx = candidates[
            position
        ]

        artist = str(
            rec_df.iloc[idx][
                "Artist(s)"
            ]
        ).strip().lower()

        if artist in used_artists:
            continue

        final_positions.append(
            position
        )

        used_artists.add(
            artist
        )

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    if len(final_positions) < n:

        ranked_remaining = sorted(
            remaining,
            key=lambda x:
                base_score[x],
            reverse=True
        )

        for position in (
            ranked_remaining
        ):

            idx = candidates[
                position
            ]

            artist = str(
                rec_df.iloc[idx][
                    "Artist(s)"
                ]
            ).strip().lower()

            if artist in used_artists:
                continue

            final_positions.append(
                position
            )

            used_artists.add(
                artist
            )

            if len(final_positions) >= n:
                break

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    results = []

    for rank, position in enumerate(
        final_positions[:n],
        start=1
    ):

        idx = candidates[
            position
        ]

        row = rec_df.iloc[
            idx
        ]

        results.append({

            "rank": rank,

            "isrc": row["ISRC"],

            "artist": row["Artist(s)"],

            "song": row["song"],

            "album": row["Album"],

            "genre": row["Genre"],

            "emotion": row[
                "emotion_predicted"
            ],

            "energy": float(
                row["Energy"]
            ),

            "danceability": float(
                row["Danceability"]
            ),

            "positiveness": float(
                row["Positiveness"]
            ),

            "popularity": float(
                row["Popularity"]
            ),

            "recommendation_score": float(
                base_score[
                    position
                ]
            )
        })

    return results
