/* 质量页：隔离行补正草稿登记、影响预览刷新、受控替换正式结果 */
(function () {
  "use strict";

  function setHint(node, text, kind) {
    if (!node) {
      return;
    }
    node.textContent = text;
    node.className = "hint" + (kind ? " " + kind : "");
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (body) {
        if (!response.ok) {
          const message = body.message || body.error || "HTTP " + response.status;
          const error = new Error(message);
          error.code = body.error;
          error.detail = body.detail;
          throw error;
        }
        return body;
      });
    });
  }

  function openEditor(button) {
    const editor = document.getElementById("correction-editor");
    const row = button.getAttribute("data-source-row");
    document.getElementById("editor-row").textContent = row;
    document.getElementById("c-source-row").value = row;
    document.getElementById("c-sensor").value = button.getAttribute("data-sensor") || "";
    const prefills = [
      ["c-time", "data-time"],
      ["c-offset", "data-offset"],
      ["c-value", "data-value"],
      ["c-unit", "data-unit"],
      ["c-seq", "data-seq"],
      ["c-note", "data-note"]
    ];
    prefills.forEach(function (pair) {
      document.getElementById(pair[0]).value = button.getAttribute(pair[1]) || "";
    });
    document.getElementById("c-quality").value = button.getAttribute("data-quality") || "good";
    setHint(document.getElementById("correction-hint"), "", "");
    editor.hidden = false;
    editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function bindEditor() {
    const table = document.getElementById("quarantine-table");
    const editor = document.getElementById("correction-editor");
    if (!table || !editor) {
      return;
    }
    table.addEventListener("click", function (event) {
      const button = event.target.closest(".js-correct");
      if (button) {
        openEditor(button);
      }
    });
    document.getElementById("c-cancel").addEventListener("click", function () {
      editor.hidden = true;
    });

    const form = document.getElementById("correction-form");
    const hint = document.getElementById("correction-hint");
    const submit = document.getElementById("c-submit");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (submit.disabled) {
        return;
      }
      const payload = {
        source_row: parseInt(document.getElementById("c-source-row").value, 10),
        sensor_id: document.getElementById("c-sensor").value,
        sampled_at: document.getElementById("c-time").value,
        offset_seconds: parseOrNull(document.getElementById("c-offset").value),
        value: nullify(document.getElementById("c-value").value),
        unit: document.getElementById("c-unit").value,
        quality: document.getElementById("c-quality").value,
        ingest_seq: parseOrNull(document.getElementById("c-seq").value),
        note: document.getElementById("c-note").value
      };
      submit.disabled = true;
      postJSON("/api/corrections", payload)
        .then(function (saved) {
          if (saved.validation && saved.validation.ok) {
            setHint(hint, "草稿已保存并通过校验：" + saved.correction_id + "，刷新后可在预览查看影响窗口", "ok");
          } else {
            setHint(
              hint,
              "草稿已保存但未通过校验（" + (saved.validation && saved.validation.code) + "），不能参与替换",
              "bad"
            );
          }
          window.setTimeout(function () {
            window.location.reload();
          }, 900);
        })
        .catch(function (error) {
          setHint(hint, "保存失败：" + error.message, "bad");
          if (window.Gridlab) {
            window.Gridlab.setStatus("补正保存失败：" + error.message, "bad");
          }
        })
        .finally(function () {
          submit.disabled = false;
        });
    });
  }

  function parseOrNull(text) {
    if (text === "" || text === null || text === undefined) {
      return null;
    }
    const value = parseInt(text, 10);
    return Number.isNaN(value) ? null : value;
  }

  function nullify(text) {
    return text === "" ? null : text;
  }

  function bindPromote() {
    const button = document.getElementById("promote-btn");
    if (!button) {
      return;
    }
    const hint = document.getElementById("promote-hint");
    button.addEventListener("click", function () {
      if (button.disabled) {
        return;
      }
      const confirmed = window.confirm(
        "将用全部校验通过的补正草稿替换正式结果。\n原始证据与隔离行保留，失败会自动回滚。是否继续？"
      );
      if (!confirmed) {
        return;
      }
      button.disabled = true;
      setHint(hint, "替换中：单事务重算网格、缺口与窗口…", "");
      postJSON("/api/corrections/promote", {})
        .then(function (result) {
          setHint(
            hint,
            "已替换为 v" + result.revision_no + "，采纳 " + result.counts.accepted +
              " 行，变更窗口 " + result.impact.window_changes.length + " 个",
            "ok"
          );
          if (window.Gridlab) {
            window.Gridlab.setStatus("补正已生效 v" + result.revision_no, "ok");
          }
          window.setTimeout(function () {
            window.location.reload();
          }, 1000);
        })
        .catch(function (error) {
          setHint(hint, "替换失败，已保留旧版结果：" + error.message, "bad");
          if (window.Gridlab) {
            window.Gridlab.setStatus("补正替换失败，旧版保留：" + error.message, "bad");
          }
          button.disabled = false;
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindEditor();
    bindPromote();
  });
})();
