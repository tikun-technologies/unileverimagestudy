"""Video study task generation.

The matrix is isolated from grid/layer/text/hybrid so a video-specific
algorithm can replace the current implementation without touching those paths.
Today it delegates to the same Golden Matrix used by grid studies.
"""

from app.services.video_matrix.video_task_generator import (
    generate_video_tasks,
    generate_video_tasks_golden,
)

__all__ = ["generate_video_tasks", "generate_video_tasks_golden"]
