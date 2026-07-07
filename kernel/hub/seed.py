"""Generate generic sample datasets so a fresh install has something real to explore.

Nothing domain-specific: a synthetic images table (media-pointer + embedding columns, so the
media/vector capabilities light up), a movies CSV, an events Parquet, and a small JSON. Called
by the CLI on first run when the workspace has no data.
"""

from __future__ import annotations

import os

import duckdb


def seed(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    con = duckdb.connect(":memory:")

    con.execute(f"""
    COPY (
      SELECT i AS id,
        'https://picsum.photos/seed/' || (i % 200) || '/320/240' AS image_url,
        320 + (i * 7) % 400 AS width, 240 + (i * 5) % 300 AS height,
        CASE WHEN i % 2 = 0 THEN 'png' ELSE 'jpg' END AS format,
        (i % 9 <> 0) AS is_valid,
        list_value(round(sin(i),4), round(cos(i),4), round(sin(i*2.0),4), round(cos(i*2.0),4),
                   round((i%10)/10.0,4), round((i%7)/7.0,4), round((i%3)/3.0,4), round((i%5)/5.0,4))::DOUBLE[] AS embedding
      FROM range(0, 500) t(i)
    ) TO '{data_dir}/images.parquet' (FORMAT PARQUET)
    """)

    con.execute(f"""
    COPY (
      SELECT i AS id, 'Movie ' || i AS title, 1980 + (i % 45) AS year,
        ['drama','comedy','action','sci-fi','doc'][1 + (i % 5)] AS genre,
        round(3.0 + (i % 25) / 10.0, 1) AS rating, 100 + (i * 37) % 90000 AS votes
      FROM range(0, 200) t(i)
    ) TO '{data_dir}/movies.csv' (FORMAT CSV, HEADER)
    """)

    con.execute(f"""
    COPY (
      SELECT i AS id, (i % 200) AS user_id,
        ['view','click','purchase','signup'][1 + (i % 4)] AS event,
        round((i % 100) * 1.5, 2) AS amount
      FROM range(0, 2000) t(i)
    ) TO '{data_dir}/events.parquet' (FORMAT PARQUET)
    """)
    con.close()


def seed_if_empty(data_dir: str) -> bool:
    if os.path.isdir(data_dir) and any(
        f.endswith((".parquet", ".csv", ".json", ".arrow", ".lance")) for f in os.listdir(data_dir)
    ):
        return False
    seed(data_dir)
    return True


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "data")
    seed(os.path.abspath(d))
    print("seeded", os.path.abspath(d))
