"""
Step 1: Parse O*NET data into a clean tasks CSV.
Merges occupation info, task statements, and importance/relevance ratings.
"""

import pandas as pd
import os

DATA_DIR = "data/db_29_1_text"
OUTPUT_DIR = "output"

def main():
    # Load raw data
    tasks = pd.read_csv(
        os.path.join(DATA_DIR, "Task Statements.txt"), sep="\t"
    )
    occupations = pd.read_csv(
        os.path.join(DATA_DIR, "Occupation Data.txt"), sep="\t"
    )
    ratings = pd.read_csv(
        os.path.join(DATA_DIR, "Task Ratings.txt"), sep="\t"
    )

    print(f"Raw tasks: {len(tasks)}")
    print(f"Occupations: {len(occupations)}")

    # Extract importance (IM) and relevance (RT) scores per task
    importance = (
        ratings[ratings["Scale ID"] == "IM"]
        .rename(columns={"Data Value": "importance_score"})[
            ["O*NET-SOC Code", "Task ID", "importance_score"]
        ]
    )
    relevance = (
        ratings[ratings["Scale ID"] == "RT"]
        .rename(columns={"Data Value": "relevance_pct"})[
            ["O*NET-SOC Code", "Task ID", "relevance_pct"]
        ]
    )

    # Merge everything
    df = tasks.merge(occupations[["O*NET-SOC Code", "Title"]], on="O*NET-SOC Code", how="left")
    df = df.merge(importance, on=["O*NET-SOC Code", "Task ID"], how="left")
    df = df.merge(relevance, on=["O*NET-SOC Code", "Task ID"], how="left")

    # Keep core tasks only
    df = df[df["Task Type"] == "Core"].copy()

    # Clean up columns
    df = df.rename(columns={
        "O*NET-SOC Code": "onet_code",
        "Task ID": "task_id",
        "Task": "task_description",
        "Title": "occupation_title",
    })
    df = df[["onet_code", "occupation_title", "task_id", "task_description",
             "importance_score", "relevance_pct"]]

    # Sort by importance
    df = df.sort_values("importance_score", ascending=False)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "onet_tasks_parsed.csv")
    df.to_csv(out_path, index=False)

    print(f"\nSaved {len(df)} core tasks across {df['onet_code'].nunique()} occupations")
    print(f"Output: {out_path}")
    print(f"\nTop 10 by importance:")
    print(df[["occupation_title", "task_description", "importance_score"]].head(10).to_string(index=False))

if __name__ == "__main__":
    main()
