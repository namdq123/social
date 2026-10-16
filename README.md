# Grok Studio

Ứng dụng tạo ảnh và video bằng OpenRouter, lưu job trong MySQL, rồi tải file và đăng lên YouTube. Ảnh dùng `tencent/hy-image-v3.5-preview`. Video mặc định là `heygen/heygen-video-1`, chọn model khác trong thiết lập video.

- Backend: FastAPI
- Frontend: React
- Database: MySQL 8.4
- Chạy bằng Docker Compose

## Chạy

```bash
docker compose up --build
```

Mở http://localhost:8080

API docs: http://localhost:8080/docs

Lần đầu MySQL khởi tạo có thể mất khoảng một phút. `docker compose logs -f backend` để xem worker.

## Cấu hình OpenRouter

1. Tạo API key trên [OpenRouter](https://openrouter.ai/keys).
2. Trong ứng dụng, mở **Cài đặt**, dán API key, bấm **Kiểm tra API key** rồi **Lưu**.
3. Base URL mặc định là `https://openrouter.ai/api/v1`. Ảnh dùng `tencent/hy-image-v3.5-preview`. Video mặc định là `heygen/heygen-video-1`; danh sách model nằm trong thiết lập video và có thể thêm model ID khác.

Mỗi lần tạo video là lệnh gọi tính phí trên tài khoản OpenRouter.

## Cấu hình YouTube

1. Trong Google Cloud, bật **YouTube Data API v3**.
2. Tạo OAuth client loại **Web application**.
3. Thêm redirect URI đúng chuỗi sau:

```text
http://localhost:8080/api/youtube/callback
```

4. Dán Client ID và Client secret vào **Cài đặt**, lưu, rồi bấm **Kết nối YouTube**.
5. Khi Google hỏi quyền, chấp nhận cả quyền tải video và xem kênh.

Video mặc định ở chế độ **Không công khai** (`unlisted`). Quota mặc định của YouTube Data API là 10.000 đơn vị/ngày; mỗi lần upload tốn khoảng 1.600 đơn vị, tức khoảng 6 video/ngày nếu chưa xin tăng quota.

## Luồng xử lý

1. LLM (`x-ai/grok-4.7`) tạo hồ sơ hai nhân vật một lần. Sau khi duyệt, hồ sơ được khóa thành `series_bible.json` và `characters/<id>/profile.json`. Ảnh nhân vật và ảnh cảnh dùng `tencent/hy-image-v3.5-preview`.
2. Vẽ ảnh mặt trước từ hồ sơ. Duyệt ảnh đó, rồi mới vẽ nghiêng, phía sau và biểu cảm. Các góc sau gửi đúng ảnh mặt trước, không vẽ lại chỉ bằng văn bản.
3. Mỗi tập mới nhận ba nhóm dữ liệu: hồ sơ đã khóa, trạng thái câu chuyện hiện tại, và tóm tắt các tập trước. Model chỉ viết tình huống, hành động và lời thoại. Duyệt tập sẽ cập nhật trạng thái và danh sách chủ đề đã dùng.
4. Ảnh cảnh được backend ghép từ hồ sơ và ảnh mặt trước đã duyệt của đúng nhân vật trong cảnh. Prompt cấm chữ trên hình.
5. Video dùng ảnh cảnh vừa tạo làm khung đầu. Prompt chỉ thêm chuyển động, giữ nguyên bố cục.
6. Worker gọi `POST /api/v1/videos`, đợi `completed`, tải mp4 về và đăng YouTube nếu đã kết nối.

Có thể tạo video trước, kết nối YouTube sau, rồi bấm **Đăng YouTube** trên job.

## Biến môi trường

Xem `.env.example`. Giá trị lưu trong màn Cài đặt nằm ở bảng `app_settings` và được ưu tiên hơn biến môi trường.

Không công bố cổng MySQL ra ngoài máy. Key và refresh token YouTube nằm trong database local.
