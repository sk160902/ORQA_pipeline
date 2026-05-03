"""
Step 91: Re-parse O*NET to include ALL task types (Core + Supplemental).

The original 1_parse_onet.py filtered to Core tasks only, which gave us 878
occupations. Including Supplemental tasks raises this to 923 occupations
(the full O*NET taxonomy has 1016 occupations; the remaining 93 are
"All Other" catch-all SOC codes with no task content at all).

Output: output/onet_tasks_parsed_full.csv
"""
import pandas as pd
import os

DATA_DIR = "data/db_29_1_text"
OUTPUT_DIR = "output"


def main():
    tasks = pd.read_csv(os.path.join(DATA_DIR, "Task Statements.txt"), sep="\t")
    occupations = pd.read_csv(os.path.join(DATA_DIR, "Occupation Data.txt"), sep="\t")
    ratings = pd.read_csv(os.path.join(DATA_DIR, "Task Ratings.txt"), sep="\t")

    print(f"Raw tasks: {len(tasks)}")
    print(f"Occupations: {len(occupations)}")

    importance = (
        ratings[ratings["Scale ID"] == "IM"]
        .rename(columns={"Data Value": "importance_score"})
        [["O*NET-SOC Code", "Task ID", "importance_score"]]
    )
    relevance = (
        ratings[ratings["Scale ID"] == "RT"]
        .rename(columns={"Data Value": "relevance_pct"})
        [["O*NET-SOC Code", "Task ID", "relevance_pct"]]
    )

    df = tasks.merge(occupations[["O*NET-SOC Code", "Title"]], on="O*NET-SOC Code", how="left")
    df = df.merge(importance, on=["O*NET-SOC Code", "Task ID"], how="left")
    df = df.merge(relevance, on=["O*NET-SOC Code", "Task ID"], how="left")

    # Keep ALL task types (both Core and Supplemental)
    print(f"Task types: {df['Task Type'].value_counts().to_dict()}")

    df = df.rename(columns={
        "O*NET-SOC Code": "onet_code",
        "Task ID": "task_id",
        "Task": "task_description",
        "Title": "occupation_title",
        "Task Type": "task_type",
    })
    df = df[["onet_code", "occupation_title", "task_id", "task_description",
             "task_type", "importance_score", "relevance_pct"]]
    df = df.sort_values("importance_score", ascending=False)

    out_path = os.path.join(OUTPUT_DIR, "onet_tasks_parsed_full.csv")
    df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")
    print(f"Total rows: {len(df)}")
    print(f"Unique occupations: {df['occupation_title'].nunique()}")
    print(f"Unique SOC codes: {df['onet_code'].nunique()}")

    # Also: occupations missing entirely (the 93)
    missing = set(occupations["O*NET-SOC Code"]) - set(df["onet_code"])
    print(f"Occupations with NO tasks (catch-all codes): {len(missing)}")
    missing_titles = occupations[occupations["O*NET-SOC Code"].isin(missing)][["O*NET-SOC Code", "Title"]]
    print(missing_titles.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
