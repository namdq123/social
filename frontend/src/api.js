async function parseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

export function errorText(data, fallback) {
  if (!data) return fallback;
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((item) => item.msg || JSON.stringify(item)).join(", ");
  }
  return fallback;
}

export async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, options);
  const data = await parseBody(response);
  if (!response.ok) {
    throw new Error(errorText(data, "Yêu cầu thất bại"));
  }
  return data;
}
