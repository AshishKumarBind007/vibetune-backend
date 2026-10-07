# VibeTune Backend

FastAPI recommendation backend for the VibeTune desktop application.

Uses the frozen VibeTune V3 recommendation engine.

Production dataset:
vibetune_recommendation_data.parquet

API endpoints:
- GET /
- GET /health
- GET /moods
- GET /energies
- GET /genres
- POST /recommend
