(function () {
  async function fetchJson(url) {
    var response = await fetch(url);
    var data = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      throw new Error(data.detail || data.error || "request failed");
    }
    return data;
  }

  function applyText(selector, value, useHtml) {
    if (!value) return;
    var element = document.querySelector(selector);
    if (!element) return;
    if (useHtml) {
      element.innerHTML = value;
    } else {
      element.textContent = value;
    }
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatMultiline(value) {
    return escapeHtml(value).replace(/\n/g, "<br>");
  }

  async function loadContentBlocks() {
    try {
      var data = await fetchJson("/api/content/get");
      var map = data.map || {};
      var page = (location.pathname.split("/").pop() || "index.html").toLowerCase();

      if (page === "" || page === "index.html") {
        applyText(".hero-text h1", map.homepage_title, true);
        applyText(".hero-text h2", map.homepage_subtitle, false);
        applyText(".hero-text p", map.homepage_intro, false);
        applyText(".message-wall-intro", map.message_wall_intro, false);
      }
    } catch (error) {
      console.warn("content blocks load failed", error);
    }
  }

  async function loadDynamicExperiences() {
    var container = document.getElementById("dynamicExperienceList");
    if (!container) return;

    try {
      var data = await fetchJson("/api/experiences");
      if (!Array.isArray(data) || !data.length) {
        container.innerHTML = '<div class="dynamic-empty">后台暂时还没有新增经历内容。</div>';
        return;
      }

      container.innerHTML = data.map(function (item) {
        var images = Array.isArray(item.images) ? item.images : [];
        var tags = Array.isArray(item.tags) ? item.tags : [];
        var gallery = images.length
          ? '<div class="dynamic-experience-gallery">' + images.map(function (image) {
              return '<img src="' + escapeHtml(image.url) + '" alt="' + escapeHtml(item.title) + '">';
            }).join("") + "</div>"
          : '<div class="dynamic-empty">该经历暂未绑定图片。</div>';

        return ''
          + '<article class="dynamic-experience-card">'
          + '  <div class="dynamic-experience-body">'
          + '    <div class="dynamic-experience-time">' + escapeHtml(item.time_label) + '</div>'
          + '    <h3>' + escapeHtml(item.title) + '</h3>'
          + '    <p>' + formatMultiline(item.description) + '</p>'
          + '    <div class="dynamic-experience-tags">' + tags.map(function (tag) {
              return '<span>' + escapeHtml(tag) + '</span>';
            }).join("") + '</div>'
          + '  </div>'
          + '  <div class="dynamic-experience-media">' + gallery + '</div>'
          + '</article>';
      }).join("");
    } catch (error) {
      container.innerHTML = '<div class="dynamic-empty">动态经历加载失败，请稍后重试。</div>';
    }
  }

  function renderMediaDocs(container, items, emptyText) {
    if (!Array.isArray(items) || !items.length) {
      container.innerHTML = '<div class="dynamic-empty">' + escapeHtml(emptyText) + '</div>';
      return;
    }

    container.innerHTML = items.map(function (item) {
      return ''
        + '<article class="dynamic-doc-card">'
        + '  <div class="dynamic-doc-icon">PDF</div>'
        + '  <div class="dynamic-doc-content">'
        + '    <div class="dynamic-doc-meta">' + escapeHtml(item.created_at || "") + '</div>'
        + '    <h3>' + escapeHtml(item.filename || item.title || "未命名文档") + '</h3>'
        + '    <p>' + escapeHtml(item.object_key || item.related_module || "CloudHome Media Center") + '</p>'
        + '    <a href="' + escapeHtml(item.url) + '" target="_blank" rel="noopener noreferrer">点击阅读</a>'
        + '  </div>'
        + '</article>';
    }).join("");
  }

  async function loadDynamicSkillDocs() {
    var container = document.getElementById("dynamicSkillDocsList");
    if (!container) return;

    try {
      var data = await fetchJson("/api/media/list?type=pdf&category=skill");
      renderMediaDocs(container, data, "后台暂时还没有上传技能 PDF。");
    } catch (error) {
      container.innerHTML = '<div class="dynamic-empty">技能文档加载失败，请稍后重试。</div>';
    }
  }

  async function loadDynamicCompetitionDocs() {
    var container = document.getElementById("dynamicCompetitionDocsList");
    if (!container) return;

    try {
      var data = await fetchJson("/api/media/list?type=pdf&category=competition");
      renderMediaDocs(container, data, "竞赛文档暂未上传。");
    } catch (error) {
      container.innerHTML = '<div class="dynamic-empty">竞赛文档加载失败，请稍后重试。</div>';
    }
  }

  async function loadHomepageMedia() {
    var photoWall = document.querySelector(".experience-photos");
    if (!photoWall) return;

    try {
      var data = await fetchJson("/api/media/list?type=image&category=life");
      if (!Array.isArray(data) || !data.length) return;
      photoWall.innerHTML = data.slice(0, 4).map(function (item) {
        return '<img src="' + escapeHtml(item.url) + '" alt="' + escapeHtml(item.filename) + '">';
      }).join("");
    } catch (error) {
      console.warn("homepage media load failed", error);
    }
  }

  loadContentBlocks();
  loadDynamicExperiences();
  loadDynamicSkillDocs();
  loadDynamicCompetitionDocs();
  loadHomepageMedia();
})();
