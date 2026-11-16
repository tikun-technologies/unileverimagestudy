import json
import sys
import time

sys.path.insert(0, ".")

from app.services.golden_task_generator import generate_grid_tasks_golden


def build_categories():
    counts = [2, 2, 2]
    return [{"category_name": chr(65 + i), "elements": [{"element_id": f"{chr(65+i)}{j+1}", "name": f"{chr(65+i)}{j+1}", "content": f"fixture-{i}-{j}", "element_type": "image"} for j in range(count)]} for i, count in enumerate(counts)]


def main():
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        result = generate_grid_tasks_golden(build_categories(), 10, seed=123, tasks_per_respondent=0)
        samples.append(time.perf_counter() - started)
    print(json.dumps({"implementation": "python-golden-matrix", "requested_respondents": 10, "generated_respondents": len(result["tasks"]), "tasks_per_respondent": result["metadata"]["tasks_per_consumer"], "capacity": result["metadata"]["capacity"], "samples_seconds": samples, "best_seconds": min(samples), "average_seconds": sum(samples) / len(samples)}))


if __name__ == "__main__":
    main()
