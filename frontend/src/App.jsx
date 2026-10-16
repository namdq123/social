import { useEffect, useMemo, useRef, useState } from "react";
import { request } from "./api";

const ACTIVE = new Set(["queued", "submitting", "generating", "downloading", "uploading"]);

const CHARACTER_STATUS = {
  queued: "Đang chờ",
  generating: "Đang tạo",
  ready: "Sẵn sàng",
  approved: "Đã duyệt",
  profile: "Chưa có ảnh",
  failed: "Lỗi",
};

const EMPTY_FORM = {
  prompt: "",
  model: "",
  customModel: "",
  resolution: "720p",
  ratio: "16:9",
  duration: 5,
  autoDuration: false,
  generateAudio: true,
  watermark: false,
  autoPublish: false,
  imageUrl: "",
  image: null,
  youtubeTitle: "",
  youtubeDescription: "",
  youtubeTags: "",
  youtubePrivacy: "unlisted",
  youtubeCategoryId: "22",
};

function formatWhen(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    day: "2-digit",
    month: "2-digit",
  }).format(date);
}

export default function App() {
  const [meta, setMeta] = useState(null);
  const [settings, setSettings] = useState(null);
  const [youtube, setYoutube] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(() => sessionStorage.getItem("projectId") || "");
  const [characters, setCharacters] = useState([]);
  const [projectName, setProjectName] = useState("");
  const [characterBusy, setCharacterBusy] = useState(false);
  const [characterError, setCharacterError] = useState("");
  const [episodes, setEpisodes] = useState([]);
  const [episodeId, setEpisodeId] = useState("");
  const episodeIdRef = useRef("");
  const [scenes, setScenes] = useState([]);
  const [episodeIdea, setEpisodeIdea] = useState("");
  const [sceneCount, setSceneCount] = useState(4);
  const [textModel, setTextModel] = useState("");
  const [customTextModel, setCustomTextModel] = useState("");
  const [continuityRules, setContinuityRules] = useState("");
  const continuityDirtyRef = useRef(false);
  const [pipelineBusy, setPipelineBusy] = useState(false);
  const [pipelineError, setPipelineError] = useState("");
  const [bible, setBible] = useState(null);
  const [bibleText, setBibleText] = useState("");
  const [bibleBusy, setBibleBusy] = useState(false);
  const [bibleError, setBibleError] = useState("");
  const bibleDirtyRef = useRef(false);
  const [storyText, setStoryText] = useState("");
  const [topicsText, setTopicsText] = useState("");
  const [storyBusy, setStoryBusy] = useState(false);
  const storyDirtyRef = useRef(false);
  const [refCharacterId, setRefCharacterId] = useState("");
  const [refPrompt, setRefPrompt] = useState("");
  const [refFile, setRefFile] = useState(null);
  const projectIdRef = useRef(sessionStorage.getItem("projectId") || "");
  const [form, setForm] = useState(EMPTY_FORM);
  const [bootError, setBootError] = useState("");
  const [formError, setFormError] = useState("");
  const [formNote, setFormNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [banner, setBanner] = useState(null);
  const [previewUrl, setPreviewUrl] = useState("");

  function chooseProject(id) {
    projectIdRef.current = id;
    setProjectId(id);
    bibleDirtyRef.current = false;
    storyDirtyRef.current = false;
    setBible(null);
    setBibleText("");
    setStoryText("");
    setTopicsText("");
    if (id) sessionStorage.setItem("projectId", id);
    else sessionStorage.removeItem("projectId");
  }

  async function refreshBoard() {
    const [nextJobs, nextYoutube, nextSettings, nextProjects] = await Promise.all([
      request("/jobs"),
      request("/youtube/status"),
      request("/settings"),
      request("/projects"),
    ]);
    setJobs(nextJobs);
    setYoutube(nextYoutube);
    setSettings(nextSettings);
    setProjects(nextProjects);
    let currentId = projectIdRef.current;
    if (!nextProjects.some((item) => item.id === currentId)) {
      currentId = nextProjects[0]?.id || "";
      projectIdRef.current = currentId;
      setProjectId(currentId);
      if (currentId) sessionStorage.setItem("projectId", currentId);
    }
    if (currentId) {
      const [nextCharacters, nextBible, nextStory] = await Promise.all([
        request(`/projects/${currentId}/characters`),
        request(`/projects/${currentId}/bible`),
        request(`/projects/${currentId}/story`),
      ]);
      setCharacters(nextCharacters);
      setBible(nextBible);
      if (!bibleDirtyRef.current) {
        setBibleText(nextBible.bible ? JSON.stringify(nextBible.bible, null, 2) : "");
      }
      if (!storyDirtyRef.current) {
        setStoryText(nextStory.current_state || "");
        setTopicsText((nextStory.used_topics || []).join("\n"));
      }
      const nextEpisodes = await request(`/projects/${currentId}/episodes`);
      setEpisodes(nextEpisodes);
      let currentEpisode = episodeIdRef.current;
      if (!nextEpisodes.some((item) => item.id === currentEpisode)) {
        currentEpisode = nextEpisodes[0]?.id || "";
        episodeIdRef.current = currentEpisode;
        setEpisodeId(currentEpisode);
      }
      if (currentEpisode) {
        setScenes(await request(`/projects/${currentId}/episodes/${currentEpisode}/scenes`));
      } else {
        setScenes([]);
      }
    } else {
      setCharacters([]);
      setBible(null);
      setBibleText("");
      setStoryText("");
      setTopicsText("");
      setEpisodes([]);
      setScenes([]);
    }
  }

  function chooseEpisode(id) {
    episodeIdRef.current = id;
    setEpisodeId(id);
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const flag = params.get("youtube");
    if (flag === "connected") {
      setBanner({ tone: "ok", text: "Đã kết nối YouTube. Video đang chờ sẽ được đăng." });
    } else if (flag === "error") {
      setBanner({
        tone: "bad",
        text: params.get("message") || "Không kết nối được YouTube.",
      });
    }
    if (flag) {
      window.history.replaceState({}, "", window.location.pathname);
    }

    let timer = 0;
    (async () => {
      try {
        const nextMeta = await request("/meta");
        setMeta(nextMeta);
        const firstModel = nextMeta.models[0];
        const firstTextModel = nextMeta.text_models?.[0];
        setTextModel((current) => current || firstTextModel?.id || "");
        setContinuityRules((current) => (continuityDirtyRef.current ? current : nextMeta.continuity_rules || ""));
        setForm((current) => ({
          ...current,
          model: current.model || firstModel?.id || "",
          duration: current.model ? current.duration : firstModel?.default_duration || 5,
          resolution: current.model
            ? current.resolution
            : firstModel?.resolutions?.[0] || current.resolution,
        }));
        await refreshBoard();
      } catch (error) {
        setBootError(error.message || "Không tải được ứng dụng.");
      }
    })();
    timer = window.setInterval(() => {
      refreshBoard().catch(() => {});
    }, 4000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!form.image) {
      setPreviewUrl("");
      return undefined;
    }
    const url = URL.createObjectURL(form.image);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [form.image]);

  const preset = useMemo(
    () => meta?.models.find((item) => item.id === form.model) || null,
    [meta, form.model],
  );

  function patchForm(partial) {
    setForm((current) => ({ ...current, ...partial }));
  }

  function onModelChange(model) {
    const next = meta.models.find((item) => item.id === model);
    const min = next?.min_duration ?? 2;
    const max = next?.max_duration ?? 30;
    const current = form.duration > 0 ? form.duration : next?.default_duration || 5;
    patchForm({
      model,
      resolution: next?.resolutions?.includes(form.resolution)
        ? form.resolution
        : next?.resolutions?.[0] || form.resolution,
      duration: Math.min(max, Math.max(min, current)),
      autoDuration: false,
    });
  }

  async function onSubmit(event) {
    event.preventDefault();
    setFormError("");
    setFormNote("");
    if (!projectIdRef.current) {
      setFormError("Hãy tạo một dự án trước.");
      return;
    }
    setSubmitting(true);
    try {
      const body = new FormData();
      body.set("project_id", projectIdRef.current);
      body.set("prompt", form.prompt);
      body.set("model", form.model);
      body.set("custom_model", form.model === "custom" ? form.customModel : "");
      body.set("resolution", resolutions.includes(form.resolution) ? form.resolution : resolutions[0] || form.resolution);
      body.set("ratio", form.ratio);
      const selected = meta?.models.find((item) => item.id === form.model);
      const audio = selected ? selected.audio !== false : true;
      body.set("duration", String(form.autoDuration ? -1 : form.duration));
      body.set("generate_audio", String(audio && form.generateAudio));
      body.set("watermark", String(form.watermark));
      body.set("auto_publish", "false");
      body.set("image_url", form.image ? "" : form.imageUrl);
      body.set("youtube_title", form.youtubeTitle);
      body.set("youtube_description", form.youtubeDescription);
      body.set("youtube_tags", form.youtubeTags);
      body.set("youtube_privacy", form.youtubePrivacy);
      body.set("youtube_category_id", form.youtubeCategoryId);
      if (form.image) body.set("image", form.image);
      await request("/jobs", { method: "POST", body });
      setFormNote("Đã đưa vào hàng đợi. Video thường xong sau vài phút.");
      await refreshBoard();
    } catch (error) {
      setFormError(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function deleteJob(id) {
    if (!window.confirm("Xóa job này và file video đã lưu?")) return;
    try {
      await request(`/jobs/${id}`, { method: "DELETE" });
      await refreshBoard();
    } catch (error) {
      setBanner({ tone: "bad", text: error.message });
    }
  }

  async function createProject(event) {
    event.preventDefault();
    const name = projectName.trim();
    if (!name) return;
    setBanner(null);
    try {
      const created = await request("/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      setProjectName("");
      chooseProject(created.id);
      await refreshBoard();
    } catch (error) {
      setBanner({ tone: "bad", text: error.message });
    }
  }

  async function removeProject() {
    if (!projectId || !window.confirm("Xóa dự án này cùng nhân vật và video đã lưu?")) return;
    try {
      await request(`/projects/${projectId}`, { method: "DELETE" });
      chooseProject("");
      await refreshBoard();
    } catch (error) {
      setBanner({ tone: "bad", text: error.message });
    }
  }

  function parseBibleText() {
    const data = JSON.parse(bibleText);
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("Hồ sơ phải là một JSON object.");
    }
    return data;
  }

  async function generateBible() {
    if (!projectId) return;
    if (!window.confirm("Tạo hồ sơ sẽ gọi LLM trên OpenRouter và tốn phí. Chỉ cần làm một lần.")) return;
    setBibleError("");
    setBibleBusy(true);
    try {
      const next = await request(`/projects/${projectId}/bible/generate`, { method: "POST" });
      bibleDirtyRef.current = false;
      setBible(next);
      setBibleText(next.bible ? JSON.stringify(next.bible, null, 2) : "");
    } catch (error) {
      setBibleError(error.message);
    } finally {
      setBibleBusy(false);
    }
  }

  async function saveBible() {
    if (!projectId) return;
    setBibleError("");
    setBibleBusy(true);
    try {
      const next = await request(`/projects/${projectId}/bible`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bible: parseBibleText() }),
      });
      bibleDirtyRef.current = false;
      setBible(next);
      setBibleText(next.bible ? JSON.stringify(next.bible, null, 2) : "");
    } catch (error) {
      setBibleError(error.message);
    } finally {
      setBibleBusy(false);
    }
  }

  async function approveBible() {
    if (!projectId) return;
    setBibleError("");
    setBibleBusy(true);
    try {
      if (bibleDirtyRef.current || bible?.status !== "draft") {
        await request(`/projects/${projectId}/bible`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ bible: parseBibleText() }),
        });
      }
      const next = await request(`/projects/${projectId}/bible/approve`, { method: "POST" });
      bibleDirtyRef.current = false;
      setBible(next);
      setBibleText(next.bible ? JSON.stringify(next.bible, null, 2) : "");
      await refreshBoard();
    } catch (error) {
      setBibleError(error.message);
    } finally {
      setBibleBusy(false);
    }
  }

  async function unlockBible() {
    if (!projectId) return;
    setBibleBusy(true);
    setBibleError("");
    try {
      const next = await request(`/projects/${projectId}/bible/unlock`, { method: "POST" });
      bibleDirtyRef.current = false;
      setBible(next);
      setBibleText(next.bible ? JSON.stringify(next.bible, null, 2) : "");
    } catch (error) {
      setBibleError(error.message);
    } finally {
      setBibleBusy(false);
    }
  }

  async function drawSheet(characterId) {
    if (!window.confirm("Vẽ mặt trước sẽ gọi API ảnh và tốn phí.")) return;
    setCharacterError("");
    try {
      await request(`/projects/${projectId}/characters/${characterId}/sheet/front`, { method: "POST" });
      await refreshBoard();
    } catch (error) {
      setCharacterError(error.message);
    }
  }

  async function approveFront(characterId) {
    setCharacterError("");
    try {
      await request(`/projects/${projectId}/characters/${characterId}/sheet/front/approve`, { method: "POST" });
      await refreshBoard();
    } catch (error) {
      setCharacterError(error.message);
    }
  }

  async function saveStory() {
    if (!projectId) return;
    setPipelineError("");
    setStoryBusy(true);
    try {
      const topics = topicsText
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
      await request(`/projects/${projectId}/story`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_state: storyText, used_topics: topics }),
      });
      storyDirtyRef.current = false;
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    } finally {
      setStoryBusy(false);
    }
  }

  async function planEpisode() {
    if (!projectId) return;
    const idea = episodeIdea.trim();
    const count = Number(sceneCount);
    const selectedText = meta?.text_models?.find((item) => item.id === textModel);
    const textLabel = textModel === "custom" ? customTextModel.trim() : selectedText?.label || "model đã chọn";
    if (!idea) {
      setPipelineError("Nhập prompt chung cho tập này.");
      return;
    }
    if (textModel === "custom" && !customTextModel.trim()) {
      setPipelineError("Nhập model ID để viết cảnh.");
      return;
    }
    if (!Number.isInteger(count) || count < 1 || count > 12) {
      setPipelineError("Số cảnh từ 1 đến 12.");
      return;
    }
    if (
      !window.confirm(
        `${textLabel} sẽ tách prompt này thành ${count} prompt cảnh, theo quy tắc dựng nối đang hiện. Việc gọi OpenRouter tốn phí. Ảnh và video từng cảnh bấm sau.`,
      )
    ) {
      return;
    }
    setPipelineError("");
    setFormNote("");
    setPipelineBusy(true);
    try {
      const episode = await request(`/projects/${projectId}/episodes/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: idea,
          scene_count: count,
          model: textModel,
          custom_model: textModel === "custom" ? customTextModel.trim() : "",
          continuity: continuityRules,
        }),
      });
      chooseEpisode(episode.id);
      setFormNote("Đã tạo prompt cho từng cảnh. Xem lại, rồi vẽ ảnh và tạo video.");
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    } finally {
      setPipelineBusy(false);
    }
  }

  async function assembleEpisode() {
    if (!projectId || !episodeId) return;
    setPipelineError("");
    setFormNote("");
    setPipelineBusy(true);
    try {
      await request(`/projects/${projectId}/episodes/${episodeId}/assemble`, { method: "POST" });
      setFormNote("Đã ghép các cảnh thành một video. File nằm ở hàng đợi, bấm Tải file để đăng YouTube.");
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    } finally {
      setPipelineBusy(false);
    }
  }

  async function approveEpisode() {
    if (!episodeId) return;
    setPipelineError("");
    setPipelineBusy(true);
    try {
      await request(`/projects/${projectId}/episodes/${episodeId}/approve`, { method: "POST" });
      storyDirtyRef.current = false;
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    } finally {
      setPipelineBusy(false);
    }
  }

  async function removeCharacter(id) {
    if (!window.confirm("Xóa nhân vật này cùng bộ ảnh tham chiếu?")) return;
    try {
      await request(`/projects/${projectId}/characters/${id}`, { method: "DELETE" });
      await refreshBoard();
    } catch (error) {
      setCharacterError(error.message);
    }
  }

  async function addReference(event) {
    event.preventDefault();
    if (!projectId || !refCharacterId) return;
    setCharacterError("");
    setCharacterBusy(true);
    try {
      const body = new FormData();
      body.set("prompt", refPrompt);
      body.set("label", "Ảnh thêm");
      body.set("aspect_ratio", "1:1");
      if (refFile) body.set("image", refFile);
      await request(`/projects/${projectId}/characters/${refCharacterId}/images`, { method: "POST", body });
      setRefPrompt("");
      setRefFile(null);
      setRefCharacterId("");
      await refreshBoard();
    } catch (error) {
      setCharacterError(error.message);
    } finally {
      setCharacterBusy(false);
    }
  }

  async function removeReference(characterId, imageId) {
    try {
      await request(`/projects/${projectId}/characters/${characterId}/images/${imageId}`, { method: "DELETE" });
      await refreshBoard();
    } catch (error) {
      setCharacterError(error.message);
    }
  }

  async function removeEpisode() {
    if (!episodeId || !window.confirm("Xóa tập này và các ảnh cảnh?")) return;
    try {
      await request(`/projects/${projectId}/episodes/${episodeId}`, { method: "DELETE" });
      chooseEpisode("");
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    }
  }

  async function drawScene(sceneId) {
    setPipelineError("");
    try {
      await request(`/projects/${projectId}/scenes/${sceneId}/generate`, { method: "POST" });
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    }
  }

  async function removeScene(sceneId) {
    if (!window.confirm("Xóa cảnh này?")) return;
    try {
      await request(`/projects/${projectId}/scenes/${sceneId}`, { method: "DELETE" });
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    }
  }

  async function createSceneVideo(scene) {
    setPipelineError("");
    setFormNote("");
    setSubmitting(true);
    try {
      const body = new FormData();
      body.set("model", form.model);
      body.set("custom_model", form.model === "custom" ? form.customModel : "");
      const resolution = resolutions.includes(form.resolution) ? form.resolution : resolutions[0] || "720p";
      body.set("resolution", resolution);
      const seconds = scene.duration_seconds > 0 ? scene.duration_seconds : form.autoDuration ? -1 : form.duration;
      body.set("duration", String(seconds));
      const selected = meta?.models.find((item) => item.id === form.model);
      const audio = selected ? selected.audio !== false : true;
      body.set("generate_audio", String(audio && form.generateAudio));
      body.set("watermark", String(form.watermark));
      body.set("auto_publish", "false");
      body.set("youtube_title", form.youtubeTitle);
      body.set("youtube_description", form.youtubeDescription);
      body.set("youtube_tags", form.youtubeTags);
      body.set("youtube_privacy", form.youtubePrivacy);
      body.set("youtube_category_id", form.youtubeCategoryId);
      await request(`/projects/${projectId}/scenes/${scene.id}/videos`, { method: "POST", body });
      setFormNote("Đã đưa video của cảnh vào hàng đợi.");
      await refreshBoard();
    } catch (error) {
      setPipelineError(error.message);
    } finally {
      setSubmitting(false);
    }
  }

  const ready = Boolean(meta && settings && youtube);
  const needsKey = settings && !settings.seedance_api_key_set;
  const needsGoogle = settings && (!settings.google_client_id || !settings.google_client_secret_set);
  const baseResolutions = preset?.resolutions || meta?.resolutions || ["720p"];
  const resolutions = baseResolutions;
  const durationMin = preset?.min_duration || 2;
  const durationMax = preset?.max_duration || 30;
  const allowAuto = preset ? preset.allow_auto_duration : true;
  const allowAudio = preset ? preset.audio !== false : true;
  const visibleJobs = jobs.filter((job) => job.project_id === projectId);
  const selectedEpisode = episodes.find((item) => item.id === episodeId);

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <p className="eyebrow">OpenRouter</p>
          <h1>Grok Studio</h1>
        </div>
        <div className="top-actions">
          <button type="button" className="ghost" onClick={() => setSettingsOpen(true)}>
            Cài đặt
          </button>
          {youtube?.connected ? (
            <div className="channel">
              <span>{youtube.channel_title || "Đã kết nối YouTube"}</span>
              <DisconnectButton onDone={refreshBoard} onError={setBanner} />
            </div>
          ) : (
            <a className="button" href="/api/youtube/connect">
              Kết nối YouTube
            </a>
          )}
        </div>
      </header>

      <main className="wrap">
        {bootError && <p className="banner bad">{bootError}</p>}
        {banner && <p className={`banner ${banner.tone}`}>{banner.text}</p>}

        {ready && (needsKey || needsGoogle || !youtube.connected) && (
          <ol className="checklist">
            <li className={needsKey ? "" : "done"}>OpenRouter API key</li>
            <li className={needsGoogle ? "" : "done"}>Google OAuth client</li>
            <li className={youtube.connected ? "done" : ""}>Kết nối kênh YouTube</li>
          </ol>
        )}

        <div className="project-bar">
          <label>
            Dự án
            <select
              value={projectId}
              onChange={(event) => {
                chooseProject(event.target.value);
                refreshBoard().catch(() => {});
              }}
            >
              {!projects.length && <option value="">Chưa có dự án</option>}
              {projects.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} ({item.character_count} nhân vật)
                </option>
              ))}
            </select>
          </label>
          <form className="inline-form" onSubmit={createProject}>
            <input
              value={projectName}
              maxLength={120}
              placeholder="Tên dự án mới"
              onChange={(event) => setProjectName(event.target.value)}
            />
            <button className="button" type="submit">
              Tạo dự án
            </button>
          </form>
          {projectId && (
            <button type="button" className="ghost danger" onClick={removeProject}>
              Xóa dự án
            </button>
          )}
        </div>

        <div className="layout">
          <section className="panel characters">
            <div className="panel-head">
              <h2>Nhân vật</h2>
              <p>
                Tạo hồ sơ một lần, duyệt để khóa ngoại hình. Ảnh mặt trước đã duyệt được gửi khi vẽ cảnh. Video chạy từ ảnh cảnh đó.
              </p>
            </div>
            {projectId && (
              <div className="character-form bible-card">
                <strong>Hồ sơ series</strong>
                <p className="hint">
                  LLM tạo hai nhân vật một lần. Sau khi duyệt, tính cách và cách nói được gắn vào từng tập; ngoại hình lấy từ hồ sơ và ảnh, không để model tự thiết kế lại.
                </p>
                {bible?.prompt && (
                  <details>
                    <summary>Prompt gửi cho model</summary>
                    <pre className="script-preview">{bible.prompt}</pre>
                  </details>
                )}
                <p className="meta-line">
                  {bible?.status === "approved" ? "Đã khóa" : bible?.status === "draft" ? "Bản nháp" : "Chưa có hồ sơ"}
                  {bible?.model ? ` · ${bible.model}` : ""}
                </p>
                <textarea
                  rows={12}
                  value={bibleText}
                  readOnly={bible?.status === "approved"}
                  placeholder="Bấm tạo hồ sơ, hoặc dán JSON để duyệt."
                  onChange={(event) => {
                    bibleDirtyRef.current = true;
                    setBibleText(event.target.value);
                  }}
                />
                {bibleError && <p className="form-error">{bibleError}</p>}
                <div className="row-actions">
                  {bible?.status !== "approved" && (
                    <>
                      <button type="button" onClick={generateBible} disabled={bibleBusy}>
                        Tạo hồ sơ bằng OpenRouter
                      </button>
                      <button type="button" onClick={saveBible} disabled={bibleBusy || !bibleText.trim()}>
                        Lưu bản nháp
                      </button>
                      <button type="button" className="button" onClick={approveBible} disabled={bibleBusy || !bibleText.trim()}>
                        Duyệt và khóa
                      </button>
                    </>
                  )}
                  {bible?.status === "approved" && (
                    <button type="button" onClick={unlockBible} disabled={bibleBusy}>
                      Mở khóa để sửa
                    </button>
                  )}
                </div>
              </div>
            )}
            {!projectId && <p className="hint">Chưa chọn dự án.</p>}
            <div className="character-list">
              {characters.map((item) => (
                <article key={item.id} className="character-card">
                  {item.has_image ? (
                    <img
                      src={`/api/projects/${item.project_id}/characters/${item.id}/image`}
                      alt={item.name}
                    />
                  ) : (
                    <div className="ph">{CHARACTER_STATUS[item.status] || item.status_label}</div>
                  )}
                  <div>
                    <strong>{item.name}</strong>
                    <p className="meta-line">
                      {(item.images || []).length} ảnh tham chiếu · {item.status_label}
                    </p>
                    <p className="prompt">{item.prompt}</p>
                    {item.bible_key ? (
                      <div className="sheet-list">
                        {(() => {
                          const image = (item.images || []).find((row) => row.slot === "front");
                          const busy = image && (image.status === "queued" || image.status === "generating");
                          return (
                            <div className="sheet-row">
                              {image?.has_image ? (
                                <img
                                  src={`/api/projects/${item.project_id}/characters/${item.id}/images/${image.id}`}
                                  alt="Mặt trước"
                                />
                              ) : (
                                <div className="ph tiny">{image?.status_label || "Chưa có"}</div>
                              )}
                              <div>
                                <strong>Mặt trước</strong>
                                {image?.status_label && <p className="meta-line">{image.status_label}</p>}
                                {image?.error_message && <p className="form-error">{image.error_message}</p>}
                                <div className="row-actions">
                                  {!busy && (
                                    <button type="button" onClick={() => drawSheet(item.id)}>
                                      {image?.has_image ? "Vẽ lại mặt trước" : "Vẽ mặt trước"}
                                    </button>
                                  )}
                                  {image?.status === "ready" && (
                                    <button type="button" onClick={() => approveFront(item.id)}>
                                      Duyệt mặt trước
                                    </button>
                                  )}
                                </div>
                              </div>
                            </div>
                          );
                        })()}
                        <p className="hint">Ảnh này được gửi khi vẽ cảnh. Video chỉ animate ảnh cảnh.</p>
                      </div>
                    ) : (
                      <>
                        <div className="ref-row">
                          {(item.images || []).map((image) =>
                            image.has_image ? (
                              <button
                                key={image.id}
                                type="button"
                                className="ref-thumb"
                                onClick={() => removeReference(item.id, image.id)}
                                title="Xóa ảnh này"
                              >
                                <img
                                  src={`/api/projects/${item.project_id}/characters/${item.id}/images/${image.id}`}
                                  alt={image.label || item.name}
                                />
                              </button>
                            ) : (
                              <div key={image.id} className="ph tiny">
                                {image.status_label}
                              </div>
                            ),
                          )}
                        </div>
                        <div className="row-actions">
                          <button type="button" onClick={() => setRefCharacterId(item.id)}>
                            Thêm ảnh
                          </button>
                        </div>
                        {refCharacterId === item.id && (
                          <form className="character-form" onSubmit={addReference}>
                            <label>
                              Mô tả góc mới
                              <input
                                value={refPrompt}
                                maxLength={2000}
                                placeholder="Nhìn nghiêng, cùng trang phục, nền đơn giản."
                                onChange={(event) => setRefPrompt(event.target.value)}
                              />
                            </label>
                            <label>
                              Hoặc tải ảnh
                              <input
                                type="file"
                                accept="image/jpeg,image/png,image/webp,image/gif,image/bmp"
                                onChange={(event) => setRefFile(event.target.files?.[0] || null)}
                              />
                            </label>
                            <button className="button" type="submit" disabled={characterBusy}>
                              {refFile ? "Lưu ảnh tham chiếu" : "Vẽ thêm từ ảnh đã có"}
                            </button>
                          </form>
                        )}
                      </>
                    )}
                    {item.error_message && <p className="form-error">{item.error_message}</p>}
                    <div className="row-actions">
                      <button type="button" className="danger" onClick={() => removeCharacter(item.id)}>
                        Xóa
                      </button>
                    </div>
                  </div>
                </article>
              ))}
            </div>
            {characterError && <p className="form-error">{characterError}</p>}
          </section>

          <section className="panel composer episodes">
            <div className="panel-head">
              <h2>Tập và cảnh</h2>
              <p>
                Nhập một prompt chung và số cảnh. Claude Haiku tách thành prompt video cho từng cảnh. Hồ sơ đã khóa và trạng thái câu chuyện được gắn sẵn.
              </p>
            </div>

            {projectId && (
              <div className="character-form">
                <label>
                  Trạng thái câu chuyện hiện tại
                  <textarea
                    rows={3}
                    maxLength={4000}
                    value={storyText}
                    onChange={(event) => {
                      storyDirtyRef.current = true;
                      setStoryText(event.target.value);
                    }}
                  />
                </label>
                <label>
                  Chủ đề đã dùng, mỗi dòng một chủ đề
                  <textarea
                    rows={3}
                    value={topicsText}
                    onChange={(event) => {
                      storyDirtyRef.current = true;
                      setTopicsText(event.target.value);
                    }}
                  />
                </label>
                <label>
                  Prompt chung
                  <textarea
                    rows={4}
                    maxLength={4000}
                    value={episodeIdea}
                    placeholder="Minh và Lan giải một câu đố vui trong lớp, cuối cùng cả hai cùng cười."
                    onChange={(event) => setEpisodeIdea(event.target.value)}
                  />
                </label>
                <label>
                  Số cảnh
                  <input
                    type="number"
                    min={1}
                    max={12}
                    value={sceneCount}
                    onChange={(event) => setSceneCount(event.target.value)}
                  />
                </label>
                <label>
                  Model viết cảnh
                  <select value={textModel} onChange={(event) => setTextModel(event.target.value)}>
                    {(meta?.text_models || []).map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                    <option value="custom">Model ID khác</option>
                  </select>
                </label>
                {textModel === "custom" && (
                  <label>
                    Model ID
                    <input
                      value={customTextModel}
                      maxLength={128}
                      placeholder="anthropic/claude-haiku-5.5"
                      onChange={(event) => setCustomTextModel(event.target.value)}
                    />
                  </label>
                )}
                <label>
                  Quy tắc dựng nối
                  <textarea
                    rows={8}
                    maxLength={8000}
                    value={continuityRules}
                    onChange={(event) => {
                      continuityDirtyRef.current = true;
                      setContinuityRules(event.target.value);
                    }}
                  />
                </label>
                <div className="row-actions">
                  <button type="button" onClick={saveStory} disabled={storyBusy}>
                    Lưu trạng thái
                  </button>
                  <button type="button" className="button" onClick={planEpisode} disabled={pipelineBusy || bible?.status !== "approved"}>
                    Tạo prompt từng cảnh
                  </button>
                </div>
                <p className="hint">Mỗi cảnh dài 5–15 giây. Quy tắc dựng nối được gửi kèm và có thể sửa trước khi tạo.</p>
              </div>
            )}

            {episodes.length > 0 && (
              <label>
                Tập đang xem
                <select
                  value={episodeId}
                  onChange={(event) => {
                    chooseEpisode(event.target.value);
                    refreshBoard().catch(() => {});
                  }}
                >
                  {episodes.map((item) => (
                    <option key={item.id} value={item.id}>
                      Tập {item.number}: {item.title} ({item.scene_count} cảnh)
                    </option>
                  ))}
                </select>
              </label>
            )}

            {selectedEpisode && (
              <>
                {selectedEpisode.summary && <p className="hint">{selectedEpisode.summary}</p>}
                <p className="hint script-preview">{selectedEpisode.script || "Tập này chưa có kịch bản."}</p>
                <div className="row-actions">
                  {selectedEpisode.status !== "approved" && selectedEpisode.summary && (
                    <button type="button" onClick={approveEpisode} disabled={pipelineBusy}>
                      Duyệt tập vào lịch sử
                    </button>
                  )}
                  {selectedEpisode.scene_count > 1 && (
                    <button type="button" onClick={assembleEpisode} disabled={pipelineBusy}>
                      Ghép thành một video
                    </button>
                  )}
                  <button type="button" className="danger" onClick={removeEpisode}>
                    Xóa tập
                  </button>
                </div>
                <div className="scene-list">
                  {scenes.map((scene) => (
                    <article key={scene.id} className="scene-card">
                      {scene.has_image ? (
                        <img
                          src={`/api/projects/${scene.project_id}/scenes/${scene.id}/image`}
                          alt={`Cảnh ${scene.sort_order}`}
                        />
                      ) : (
                        <div className="ph">{scene.status_label}</div>
                      )}
                      <div>
                        <strong>
                          Cảnh {scene.sort_order} · {scene.aspect_ratio}
                          {scene.duration_seconds > 0 ? ` · ${scene.duration_seconds}s` : ""}
                          {scene.join === "continue" ? " · Tiếp tục" : ""}
                          {scene.join === "new_angle" ? " · Đổi góc" : ""}
                        </strong>
                        {scene.cut_note && <p className="hint">{scene.cut_note}</p>}
                        <p className="prompt">{scene.prompt}</p>
                        {scene.error_message && <p className="form-error">{scene.error_message}</p>}
                        <div className="row-actions">
                          {scene.status !== "queued" && scene.status !== "generating" && (
                            <button type="button" onClick={() => drawScene(scene.id)}>
                              {scene.has_image ? "Vẽ lại ảnh cảnh" : "Vẽ ảnh cảnh"}
                            </button>
                          )}
                          {scene.has_image && (
                            <button type="button" onClick={() => createSceneVideo(scene)} disabled={submitting}>
                              Tạo video từ ảnh này
                            </button>
                          )}
                          <button type="button" className="danger" onClick={() => removeScene(scene.id)}>
                            Xóa
                          </button>
                        </div>
                      </div>
                    </article>
                  ))}
                </div>
              </>
            )}

            <details className="youtube-fields">
              <summary>Thiết lập video</summary>
              <div className="grid-2">
                <label>
                  Model
                  <select value={form.model} onChange={(event) => onModelChange(event.target.value)}>
                    {(meta?.models || []).map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                    <option value="custom">Model ID khác</option>
                  </select>
                </label>
                <label>
                  Độ phân giải
                  <select
                    value={resolutions.includes(form.resolution) ? form.resolution : resolutions[0]}
                    onChange={(event) => patchForm({ resolution: event.target.value })}
                  >
                    {resolutions.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {form.model === "custom" && (
                <label>
                  Model ID
                  <input
                    required
                    value={form.customModel}
                    onChange={(event) => patchForm({ customModel: event.target.value })}
                  />
                </label>
              )}
              <label>
                Thời lượng (giây)
                <input
                  type="number"
                  min={durationMin}
                  max={durationMax}
                  disabled={form.autoDuration}
                  value={form.duration}
                  onChange={(event) => patchForm({ duration: Number(event.target.value) })}
                />
              </label>
              <div className="checks">
                {allowAudio && (
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={form.generateAudio}
                      onChange={(event) => patchForm({ generateAudio: event.target.checked })}
                    />
                    Tạo tiếng
                  </label>
                )}
              </div>
            </details>
            <p className="hint">
              Bấm vẽ ảnh cảnh sẽ gọi API ảnh và tốn phí. Video chỉ chạy sau khi ảnh cảnh đã có đủ nhân vật. Tải file rồi dán tiêu đề và mô tả lên YouTube.
            </p>
            {pipelineError && <p className="form-error">{pipelineError}</p>}
            {formNote && <p className="form-note">{formNote}</p>}
          </section>

          <section className="board">
            <div className="panel-head">
              <h2>Hàng đợi</h2>
              <p>{visibleJobs.length ? `${visibleJobs.length} video` : "Chưa có video nào trong dự án này."}</p>
            </div>
            {!visibleJobs.length && ready && (
              <div className="empty">
                {projectId
                  ? "Vẽ ảnh cảnh ở giữa, rồi bấm Tạo video từ ảnh này."
                  : "Tạo một dự án, lưu ảnh nhân vật, rồi viết kịch bản từng tập."}
              </div>
            )}
            <div className="cards">
              {visibleJobs.map((job) => (
                <article key={job.id} className={`card status-${job.status}`}>
                  <header>
                    <div>
                      <strong>{job.youtube_title}</strong>
                      <p>{formatWhen(job.created_at)}</p>
                    </div>
                    <span className={`pill ${ACTIVE.has(job.status) ? "live" : ""}`}>
                      {job.status_label}
                    </span>
                  </header>
                  <p className="prompt">{job.prompt}</p>
                  <p className="meta-line">
                    {job.model} · {job.resolution} · {job.ratio} ·{" "}
                    {job.duration === -1 ? "thời lượng tự động" : `${job.duration}s`}
                    {job.seedance_status ? ` · ${job.seedance_status}` : ""}
                  </p>
                  {job.error_message && <p className="form-error">{job.error_message}</p>}
                  {job.has_video && (
                    <video src={`/api/jobs/${job.id}/video`} controls playsInline preload="metadata" />
                  )}
                  {job.has_video && (
                    <div className="paste-box">
                      <CopyField label="Tiêu đề" value={job.youtube_title} />
                      <CopyField label="Mô tả" value={job.youtube_description} rows={6} />
                    </div>
                  )}
                  <footer>
                    {job.youtube_url && (
                      <a href={job.youtube_url} target="_blank" rel="noreferrer">
                        Mở trên YouTube
                      </a>
                    )}
                    {job.has_video && (
                      <a href={`/api/jobs/${job.id}/video?download=1`}>Tải file</a>
                    )}
                    {!ACTIVE.has(job.status) && (
                      <button type="button" className="danger" onClick={() => deleteJob(job.id)}>
                        Xóa
                      </button>
                    )}
                  </footer>
                </article>
              ))}
            </div>
          </section>
        </div>
      </main>

      {settingsOpen && settings && (
        <SettingsDialog
          settings={settings}
          onClose={() => setSettingsOpen(false)}
          onSaved={async (next) => {
            setSettings(next);
            await refreshBoard();
          }}
        />
      )}
    </div>
  );
}

function CopyField({ label, value, rows = 2 }) {
  const [note, setNote] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(value || "");
      setNote("Đã sao chép");
    } catch {
      setNote("Hãy chọn chữ rồi sao chép");
    }
  }
  if (!value) return null;
  return (
    <label className="paste-field">
      {label}
      <textarea readOnly rows={rows} value={value} />
      <button type="button" onClick={copy}>
        {note || "Sao chép"}
      </button>
    </label>
  );
}

function DisconnectButton({ onDone, onError }) {
  async function disconnect() {
    try {
      await request("/youtube/account", { method: "DELETE" });
      await onDone();
    } catch (error) {
      onError({ tone: "bad", text: error.message });
    }
  }
  return (
    <button type="button" className="textish" onClick={disconnect}>
      Ngắt
    </button>
  );
}

function SettingsDialog({ settings, onClose, onSaved }) {
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(settings.seedance_base_url || "");
  const [clientId, setClientId] = useState(settings.google_client_id || "");
  const [clientSecret, setClientSecret] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const payload = { seedance_base_url: baseUrl, google_client_id: clientId };
      if (apiKey.trim()) payload.seedance_api_key = apiKey.trim();
      if (clientSecret.trim()) payload.google_client_secret = clientSecret.trim();
      const next = await request("/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setApiKey("");
      setClientSecret("");
      setMessage("Đã lưu.");
      await onSaved(next);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function clearKey(field) {
    setBusy(true);
    setError("");
    try {
      const next = await request("/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: true }),
      });
      setMessage("Đã xóa giá trị đã lưu. Ứng dụng dùng lại biến môi trường nếu có.");
      await onSaved(next);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function testKey() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await request("/settings/test-seedance", { method: "POST" });
      setMessage(result.message);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function copyUri() {
    try {
      await navigator.clipboard.writeText(settings.redirect_uri);
      setMessage("Đã chép redirect URI.");
    } catch {
      setError("Không chép được. Hãy chọn và copy thủ công.");
    }
  }

  return (
    <div className="modal-back" onMouseDown={onClose}>
      <form className="modal" onMouseDown={(event) => event.stopPropagation()} onSubmit={save}>
        <div className="panel-head">
          <h2>Cài đặt</h2>
          <button type="button" className="ghost" onClick={onClose}>
            Đóng
          </button>
        </div>
        <label>
          OpenRouter API key {settings.seedance_api_key_set ? "(đã lưu)" : ""}
          <input
            type="password"
            value={apiKey}
            autoComplete="off"
            placeholder={settings.seedance_api_key_set ? "Nhập key mới để thay" : "sk-or-v1-..."}
            onChange={(event) => setApiKey(event.target.value)}
          />
        </label>
        <label>
          OpenRouter base URL
          <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
        </label>
        <div className="inline-actions">
          <button type="button" className="ghost" disabled={busy} onClick={testKey}>
            Kiểm tra API key
          </button>
          {settings.seedance_api_key_set && (
            <button type="button" className="textish" disabled={busy} onClick={() => clearKey("clear_seedance_api_key")}>
              Xóa key đã lưu
            </button>
          )}
        </div>
        <label>
          Google client ID
          <input value={clientId} onChange={(event) => setClientId(event.target.value)} />
        </label>
        <label>
          Google client secret {settings.google_client_secret_set ? "(đã lưu)" : ""}
          <input
            type="password"
            value={clientSecret}
            autoComplete="off"
            placeholder={settings.google_client_secret_set ? "Nhập secret mới để thay" : "Client secret"}
            onChange={(event) => setClientSecret(event.target.value)}
          />
        </label>
        <label>
          Redirect URI đăng ký trên Google Cloud
          <input readOnly value={settings.redirect_uri} />
        </label>
        <div className="inline-actions">
          <button type="button" className="ghost" onClick={copyUri}>
            Chép redirect URI
          </button>
          {settings.google_client_secret_set && (
            <button
              type="button"
              className="textish"
              disabled={busy}
              onClick={() => clearKey("clear_google_client_secret")}
            >
              Xóa secret đã lưu
            </button>
          )}
        </div>
        {error && <p className="form-error">{error}</p>}
        {message && <p className="form-note">{message}</p>}
        <button className="button submit" type="submit" disabled={busy}>
          {busy ? "Đang lưu..." : "Lưu cài đặt"}
        </button>
      </form>
    </div>
  );
}
