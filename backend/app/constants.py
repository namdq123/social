RATIOS = ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"]
RESOLUTIONS = ["480p", "720p", "768p", "1080p", "2K", "4k"]
IMAGE_MODEL = "tencent/hy-image-v3.5-preview"
PRIVACY = ["public", "unlisted", "private"]

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

TEXT_MODEL_PRESETS = [
    {"id": "anthropic/claude-haiku-5.5", "label": "Claude Haiku 5.5"},
    {"id": "x-ai/grok-4.7", "label": "Grok 4.7"},
]

CONTINUITY_RULES = """Thiết kế các cảnh để dựng nối mượt:

- Phân biệt cảnh tiếp tục hành động (join = continue) và cảnh đổi góc hoặc địa điểm (join = new_angle).
- Mỗi cảnh phải có start_state và end_state rõ ràng.
- Với cảnh tiếp tục: start_state phải khớp end_state của cảnh trước về tư thế, tay đang dùng, đạo cụ và hướng chuyển động.
- Với cảnh đổi góc: cut_note nêu điểm cắt hợp lý và chủ thể của cảnh tiếp theo.
- Không kết thúc mọi cảnh bằng việc nhân vật đứng yên hoặc nhìn camera.
- Không cắt giữa một từ hoặc khi hành động chưa đủ rõ.
- Giữ một đoạn đệm ngắn ở đầu và cuối clip để có thể cắt khi dựng. Hành động chính và lời thoại phải kết thúc trước đoạn đệm cuối."""

MODEL_PRESETS = [
    {
        "id": "bytedance/seedance-2.5",
        "label": "Seedance 2.5",
        "min_duration": 4,
        "max_duration": 30,
        "allow_auto_duration": False,
        "audio": True,
        "resolutions": ["720p", "480p"],
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "default_duration": 8,
    },
    {
        "id": "heygen/heygen-video-1",
        "label": "HeyGen Video 1",
        "min_duration": 5,
        "max_duration": 15,
        "allow_auto_duration": False,
        "audio": False,
        "resolutions": ["768p", "480p", "2K"],
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "default_duration": 5,
    },
    {
        "id": "x-ai/grok-imagine-video-1.5",
        "label": "Grok Imagine Video 1.5",
        "min_duration": 1,
        "max_duration": 15,
        "allow_auto_duration": False,
        "audio": True,
        "resolutions": ["480p", "720p", "1080p"],
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"],
        "default_duration": 5,
    },
]

CATEGORIES = [
    {"id": "22", "label": "Người và blog"},
    {"id": "24", "label": "Giải trí"},
    {"id": "1", "label": "Phim và hoạt hình"},
    {"id": "23", "label": "Hài"},
    {"id": "26", "label": "Hướng dẫn và phong cách"},
    {"id": "28", "label": "Khoa học và công nghệ"},
    {"id": "27", "label": "Giáo dục"},
    {"id": "20", "label": "Trò chơi"},
    {"id": "10", "label": "Âm nhạc"},
]

ACTIVE_STATUSES = {
    "queued",
    "submitting",
    "generating",
    "downloading",
    "uploading",
}

TERMINAL_STATUSES = {"published", "failed"}

STATUS_LABELS = {
    "queued": "Đang chờ",
    "submitting": "Đang gửi yêu cầu",
    "generating": "Đang tạo video",
    "downloading": "Đang tải video",
    "generated": "Đã tạo",
    "uploading": "Đang đăng YouTube",
    "published": "Đã đăng",
    "failed": "Lỗi tạo video",
    "upload_failed": "Lỗi đăng YouTube",
}
