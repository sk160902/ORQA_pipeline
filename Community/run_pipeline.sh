#!/bin/bash
# Full pipeline for ONET-based verifiable Q&A benchmark generation
#
# Prerequisites:
#   export OPENAI_API_KEY=sk-...
#   pip install openai pandas requests
#
# Usage:
#   bash run_pipeline.sh          # Full pipeline
#   bash run_pipeline.sh --test   # Test run with small sample

set -e

echo "=========================================="
echo "O*NET Verifiable Q&A Benchmark Pipeline"
echo "=========================================="

# Check for API key
if [ -z "$OPENAI_API_KEY" ]; then
    echo "ERROR: OPENAI_API_KEY not set"
    echo "  export OPENAI_API_KEY=sk-..."
    exit 1
fi

TEST_MODE=""
if [ "$1" == "--test" ]; then
    TEST_MODE="yes"
    echo "Running in TEST mode (small sample)"
fi

# Step 0: Convert CentaurBench tasks to verifiable format (Abhishek's Task #1)
echo ""
echo "Step 0: Converting CentaurBench tasks to verifiable Q&A..."
if [ "$TEST_MODE" ]; then
    python3 5_centaur_to_verifiable.py --tasks menu_planning tutoring --n-scenarios 1
else
    python3 5_centaur_to_verifiable.py --tasks all --n-scenarios 3
fi

# Step 1: Parse O*NET data
echo ""
echo "Step 1: Parsing O*NET data..."
python3 1_parse_onet.py

# Step 2: Grade tasks (sample in test mode)
echo ""
echo "Step 2: Grading tasks on simulatability & evaluability..."
if [ "$TEST_MODE" ]; then
    python3 2_grade_tasks.py --sample 50
else
    python3 2_grade_tasks.py --sample 500  # Start with 500, can scale up
fi

# Step 3: Generate verifiable Q&A
echo ""
echo "Step 3: Generating verifiable Q&A..."
if [ "$TEST_MODE" ]; then
    python3 3_generate_qa.py --n-occupations 2 --questions-per-task 3
else
    python3 3_generate_qa.py --n-occupations 5 --questions-per-task 5
fi

# Step 4: Evaluate LLMs
echo ""
echo "Step 4: Evaluating LLMs..."
if [ "$TEST_MODE" ]; then
    python3 4_evaluate_llms.py --models gpt-4o-mini
else
    python3 4_evaluate_llms.py --models gpt-4o gpt-4o-mini gpt-3.5-turbo
fi

echo ""
echo "=========================================="
echo "Pipeline complete! Check output/ directory"
echo "=========================================="
