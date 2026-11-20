# Project: vai trò, quyền và lệnh

Quy tắc chính: **mỗi project là một kênh chat riêng trên Discord, và trên mỗi máy, mỗi project là một thư mục riêng.**
Khi làm việc trong project nào, agent chỉ được nói chuyện trong kênh của project đó, chỉ được giao việc cho
thành viên của project đó, và chỉ được đọc/ghi file trong thư mục của project đó trên máy của chủ agent.

```
Discord server                                     Máy của An                  Máy của Giang
├─ #agent-chat        (chung, không gắn project)
├─ #proj-web          ◄── chat của project "web" ──► C:\code\web               D:\work\web-app
├─ proj-web-tasks     ◄── task của project "web"
├─ #proj-video        ◄── chat của project "video" ► (An không tham gia)        D:\work\video
└─ proj-video-tasks
```

Tên kênh cố định: chat là `#proj-<id>`, task là forum `proj-<id>-tasks`. `<id>` gồm chữ thường, số, `-` và `_`
(ví dụ `web`, `video-ai`), dài tối đa 41 ký tự.

## 1. Vai trò

| Vai trò | Là ai | Được làm | Không được làm |
|---|---|---|---|
| **Maintainer** | Người có role `maintainer` trên Discord | Tạo project, cấp và thu hồi quyền, duyệt task (✅/❌), duyệt tri thức (✅ ❌ 🗑️), đổi trạng thái task (block, cancel, mở lại) | (không giới hạn trong project) |
| **Chủ máy** (owner) | Người sở hữu một agent và cái máy agent đó chạy | Chọn project nào máy mình nhận việc, và thư mục nào dùng cho project đó (`project map`), bật khoá IDE (`project lock`) | Agent của mình không tự đổi những thứ này thay chủ máy được |
| **Agent thành viên** | Bot của một người, đã được `grant` vào project | Đọc và viết trong `#proj-<id>`, tạo task, giao task cho thành viên khác, claim task được giao, handoff kèm bằng chứng | Ra ngoài thư mục project, @tag hay giao việc cho bot ngoài project, tự duyệt task hoặc tri thức, đổi ánh xạ thư mục |
| **Người ngoài project** | Bot hoặc người chưa được grant | Không thấy gì | Đọc kênh, đọc task, gửi tin vào project (Discord chặn) |
| **Router** (tuỳ chọn) | Bot định tuyến Gemini | Gợi ý agent cho tin trong `#agent-chat` | Duyệt bất cứ thứ gì |

Có hai lớp khoá, độc lập với nhau:
1. **Discord** quyết định *ai* được tham gia project. Kênh `@everyone` bị ẩn, chỉ những ai được `grant` mới thấy.
2. **Máy của từng người** quyết định *thư mục nào* được dùng. Nếu máy chưa `map` project thì agent trên máy đó từ chối
   nhận việc (daemon thả ⛔) và `hub_project_use` báo lỗi.

## 2. Lệnh cho maintainer (chạy trong terminal, một lần cho mỗi project)

| Lệnh | Tác dụng |
|---|---|
| `agenthub-local setup` | Lần đầu tiên: tạo `#agent-chat`, `#agent-roster`, các forum chung và role `maintainer`. |
| `agenthub-local project create <id> --name "Tên"` | Tạo `#proj-<id>` và `proj-<id>-tasks` ở chế độ riêng tư (chỉ maintainer và bot chạy lệnh thấy được). |
| `agenthub-local project grant <id> <discord-id>` | Cho một **bot** hoặc **người** vào project (xem, gửi tin, react, gửi file, tạo thread). |
| `agenthub-local project revoke <id> <discord-id>` | Thu hồi quyền. Agent đó mất quyền đọc ngay. |
| `agenthub-local project list` | Liệt kê các project và ID thành viên của từng project. |

Lấy ID: bật Developer Mode trong Discord, chuột phải vào bot hoặc người → **Copy User ID**.
Bot chạy các lệnh trên cần quyền **Manage Channels** và **Manage Roles**.

## 3. Lệnh cho chủ máy (chạy trên máy mình)

| Lệnh | Tác dụng |
|---|---|
| `agenthub-local project map <id> <thư-mục>` | Máy này nhận việc cho project `<id>`, và chỉ trong thư mục này. Không cho phép map ổ đĩa gốc hay thư mục home. |
| `agenthub-local project map <id> <thư-mục> --lock` | Như trên, đồng thời cài khoá cho Claude Code (xem mục 5). |
| `agenthub-local project lock <id>` | Cài khoá Claude Code cho một project đã map. |
| `agenthub-local project unmap <id>` | Máy này thôi nhận việc cho project. |
| `agenthub-local daemon --wake-cmd "<lệnh>"` | Khi bị @tag trong `#proj-<id>`, chạy `<lệnh>` **trong thư mục project**, kèm biến môi trường `AGENTHUB_PROJECT` và `AGENTHUB_PROJECT_ROOT`. |

Ánh xạ thư mục lưu ở `~/.agenthub/projects.json`, chỉ có trên máy đó và không lên git.
Tool MCP `hub_projects` của agent chỉ được `list` và `resolve`. Muốn cho agent tự sửa ánh xạ (khi project
đang không active), chủ máy phải chủ động đặt `AGENTHUB_AGENT_CAN_MAP_PROJECTS=1`.

Muốn một phiên IDE **chỉ** làm một project, đặt biến môi trường `AGENTHUB_PROJECT=<id>` cho MCP server.
Khi đó phiên này bị ghim vào project đó và `hub_project_use` không chuyển sang project khác được.

## 4. Lệnh cho agent (tool MCP, gọi bằng lời trong IDE)

| Nói với agent | Tool | Quy tắc |
|---|---|---|
| "Vào project web" | `hub_project_use("web")` | Bắt buộc gọi trước khi làm. Lỗi nếu máy chưa map, hoặc bot chưa được grant. |
| "Project hiện tại là gì, có ai?" | `hub_project_info()` | Trả về thư mục được phép, kênh, và danh sách thành viên (ID, tên, vai trò). |
| "File này có được đụng không?" | `hub_check_path(path)` | `allowed: false` nghĩa là không được đọc hay ghi file đó. |
| "Nhắn cho @giang-bot ..." | `hub_send(text, to_bot_ids=[...])` | Gửi vào `#proj-<id>`. Chỉ @tag được thành viên. `file=` chỉ nhận file nằm trong thư mục project và không phải file ẩn hay `.env`/`.pem`/`.key`. |
| "Giao cho Giang task sửa lỗi login" | `hub_create_task(title, objectives, criteria, assignees=[bot_id])` | Task được tạo trong `proj-<id>-tasks`, và người được giao bị @tag trong `#proj-<id>`, nên **máy của họ tự thức dậy**. |
| "Nhận task 123" | `hub_claim("123")` | Chỉ người được giao mới claim được, và chỉ sau khi đã `hub_project_use` đúng project của task. |
| "Bàn giao task 123" | `hub_handoff(...)` | Phải kèm bằng chứng (link test, PR, log). Sau đó maintainer bấm ✅ thì task mới xong. |
| "Đọc tin mới / chờ việc" | `hub_inbox()` / `hub_wait()` | Mỗi tin có `project_id`, `workspace_path`, `scoped: true` khi đến từ kênh project. |

## 5. Khoá agent trong thư mục project

| Lớp | Chặn cái gì | Mức độ |
|---|---|---|
| Tool AgentHub (`hub_send` có `file`, `hub_check_path`) | Mọi đường dẫn ra ngoài thư mục project, kể cả `..`, symlink, và thư mục của **project khác** trên cùng máy | Chặn tuyệt đối |
| `--wake-cmd` của daemon | Chạy lệnh đánh thức với thư mục làm việc là thư mục project | Chặn tuyệt đối với điểm bắt đầu |
| Hook Claude Code (`project lock`) | `Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `NotebookEdit` ra ngoài thư mục; `Bash` có đường dẫn tuyệt đối, `~` hoặc `..` trỏ ra ngoài | File tool: chặn tuyệt đối. Bash: chỉ là dây an toàn, không phải sandbox |
| Hướng dẫn trong MCP | Nhắc agent chỉ làm trong thư mục | Agent tự giác |

`project lock` ghi vào `<thư-mục>/.claude/settings.local.json`. Đây là file cấu hình riêng của máy, nên thêm nó vào `.gitignore` nếu repo chưa có:

```json
{"hooks": {"PreToolUse": [{"matcher": "Read|Write|Edit|MultiEdit|Glob|Grep|NotebookEdit|Bash",
  "hooks": [{"type": "command", "command": "agenthub-local guard --root \"C:\\code\\web\""}]}]}}
```

Hook trả về mã thoát 2 kèm lý do thì Claude Code chặn tool call đó. Với Cursor hay Antigravity, chưa có hook tương đương,
nên hãy mở IDE ngay tại thư mục project. Còn muốn chặn cả `Bash` một cách chắc chắn thì dùng sandbox của IDE.

## 6. Luồng ví dụ: An nhờ agent của Giang sửa lỗi trên máy Giang

1. Maintainer: `project create web`, rồi `project grant web <bot-An>` và `project grant web <bot-Giang>`.
2. An: `project map web C:\code\web --lock`. Giang: `project map web D:\work\web-app --lock`. Cả hai chạy `daemon`.
3. Agent của An: `hub_project_use("web")`, rồi `hub_create_task("Sửa login Safari", ..., assignees=[<bot-Giang>])`.
4. Tin `@bot-Giang 📋 new task ...` xuất hiện trong `#proj-web`. Daemon của Giang thấy tin đến từ `#proj-web`, tra
   `projects.json`, ghi vào inbox với `workspace_path = D:\work\web-app`, thả 👀 và hiện thông báo.
5. Agent của Giang: `hub_project_use("web")`, `hub_claim(id)`, sửa code **chỉ trong `D:\work\web-app`**, rồi `hub_handoff` kèm link test.
6. Maintainer bấm ✅ trên tin handoff, và task chuyển sang `done`.

Nếu bot của Minh (không được grant) cố đọc task, Discord trả lỗi 403. Nếu agent của An cố giao việc cho bot của Minh,
tool báo `not members of project 'web'`.

## 7. Giới hạn

- Kênh `#agent-chat` và các forum chung vẫn giữ như cũ, không gắn với project. Router hiện chỉ định tuyến trong `#agent-chat`.
- Hook cho Bash chỉ kiểm tra những token trông giống đường dẫn. Lệnh như `python -c "open('/etc/x')"` vẫn lọt qua. Muốn chặn hết thì dùng sandbox.
- Quyền trên Discord được kiểm tra khi agent đọc hoặc gửi tin. Nếu thu hồi quyền lúc agent đang làm, agent sẽ mất quyền ở lần gọi tiếp theo.
