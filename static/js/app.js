const CONFIG_KEYS = ['apiBase', 'apiKey', 'modelName'];
let currentPdf = null;
let currentFile = null;

const APP_PREFIX = (() => {
    const marker = '/static/';
    const idx = window.location.pathname.indexOf(marker);
    return idx >= 0 ? window.location.pathname.slice(0, idx) : '';
})();

const withPrefix = (path) => `${APP_PREFIX}${path}`;

document.addEventListener('DOMContentLoaded', () => {
    console.log('[系统] 即刻目录启动...');

    loadConfig();

    const dropArea = document.getElementById('dropArea');
    const fileInput = document.getElementById('pdfFileInput');
    const saveConfigBtn = document.getElementById('saveConfigBtn');
    const renderBtn = document.getElementById('renderBtn');

    saveConfigBtn.addEventListener('click', saveConfig);

    dropArea.addEventListener('click', () => fileInput.click());

    dropArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropArea.classList.add('dragover');
    });

    dropArea.addEventListener('dragleave', () => {
        dropArea.classList.remove('dragover');
    });

    dropArea.addEventListener('drop', (e) => {
        e.preventDefault();
        dropArea.classList.remove('dragover');
        if (e.dataTransfer.files.length) {
            handlePdfFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length) {
            handlePdfFile(e.target.files[0]);
        }
    });

    renderBtn.addEventListener('click', renderSelectedPages);
});

function loadConfig() {
    CONFIG_KEYS.forEach(key => {
        const val = localStorage.getItem(`pdf_toc_${key}`);
        if (val) {
            document.getElementById(key).value = val;
        }
    });
}

function saveConfig() {
    CONFIG_KEYS.forEach(key => {
        const val = document.getElementById(key).value;
        localStorage.setItem(`pdf_toc_${key}`, val);
    });

    const btn = document.getElementById('saveConfigBtn');
    const originalText = btn.innerText;
    btn.innerText = '[ 已保存 ]';
    btn.style.borderColor = '#fff';
    btn.style.color = '#fff';

    setTimeout(() => {
        btn.innerText = originalText;
        btn.style.borderColor = '';
        btn.style.color = '';
    }, 2000);
}

function handlePdfFile(file) {
    if (file.type !== 'application/pdf') {
        alert('错误：仅支持 PDF 文件。');
        return;
    }

    currentFile = file;
    document.getElementById('pdfInfo').classList.remove('hidden');
    document.getElementById('fileName').innerText = file.name;
    document.getElementById('filePages').innerText = '加载中...';

    const fileReader = new FileReader();
    fileReader.onload = async function () {
        const typedarray = new Uint8Array(this.result);
        try {
            currentPdf = await pdfjsLib.getDocument(typedarray).promise;
            document.getElementById('filePages').innerText = currentPdf.numPages;
            console.log(`[文件] 已加载 ${file.name} - 共 ${currentPdf.numPages} 页`);

            const startInput = document.getElementById('startPage');
            const endInput = document.getElementById('endPage');
            const renderBtn = document.getElementById('renderBtn');
            const recognizeBtn = document.getElementById('recognizeBtn');
            const generatePdfBtn = document.getElementById('generatePdfBtn');
            const splitStart = document.getElementById('splitStart');
            const splitEnd = document.getElementById('splitEnd');
            const splitBtn = document.getElementById('splitBtn');

            startInput.disabled = false;
            endInput.disabled = false;
            renderBtn.disabled = false;
            recognizeBtn.disabled = false;
            generatePdfBtn.disabled = false;
            splitStart.disabled = false;
            splitEnd.disabled = false;
            splitBtn.disabled = false;

            startInput.max = currentPdf.numPages;
            endInput.max = currentPdf.numPages;
            splitStart.max = currentPdf.numPages;
            splitEnd.max = currentPdf.numPages;

            startInput.value = 1;
            endInput.value = Math.min(3, currentPdf.numPages);
            splitStart.value = 1;
            splitEnd.value = currentPdf.numPages;

            renderSelectedPages();
        } catch (err) {
            console.error('[错误] PDF 解析失败', err);
            document.getElementById('filePages').innerText = '解析失败';
            currentPdf = null;
        }
    };

    fileReader.readAsArrayBuffer(file);
}

async function renderSelectedPages() {
    if (!currentPdf) return;

    const startObj = document.getElementById('startPage');
    const endObj = document.getElementById('endPage');

    let start = parseInt(startObj.value, 10);
    let end = parseInt(endObj.value, 10);

    if (start > end) {
        const temp = start;
        start = end;
        end = temp;
        startObj.value = start;
        endObj.value = end;
    }

    if (start < 1) start = 1;
    if (end > currentPdf.numPages) end = currentPdf.numPages;

    const previewBox = document.getElementById('previewContent');
    previewBox.innerHTML = '';
    console.log(`[系统] 正在渲染第 ${start} 到 ${end} 页`);

    for (let i = start; i <= end; i++) {
        const page = await currentPdf.getPage(i);
        const viewport = page.getViewport({ scale: 1.5 });

        const canvas = document.createElement('canvas');
        canvas.dataset.page = i;
        const context = canvas.getContext('2d');
        canvas.height = viewport.height;
        canvas.width = viewport.width;

        await page.render({ canvasContext: context, viewport }).promise;
        previewBox.appendChild(canvas);
    }
}

document.getElementById('recognizeBtn').addEventListener('click', async () => {
    const btn = document.getElementById('recognizeBtn');
    const editor = document.getElementById('tocEditor');

    const apiKey = document.getElementById('apiKey').value;
    const apiBase = document.getElementById('apiBase').value || 'https://api.openai.com/v1';
    const modelName = document.getElementById('modelName').value || 'gpt-4o-mini';

    if (!apiKey) {
        alert('错误：请先在配置中填写 API_KEY。');
        return;
    }

    const canvases = document.querySelectorAll('#previewContent canvas');
    if (canvases.length === 0) {
        alert('错误：没有可识别的预览页面。');
        return;
    }

    const imagesBase64 = Array.from(canvases).map(c => c.toDataURL('image/jpeg', 0.8));

    btn.innerText = '[ 识别中... ]';
    btn.disabled = true;
    editor.value = '// 请稍候，正在发送页面到模型...\n';

    try {
        const response = await fetch(withPrefix('/api/recognize'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_key: apiKey,
                api_base: apiBase,
                model_name: modelName,
                images_base64: imagesBase64
            })
        });

        const data = await response.json();

        if (data.status === 'ok') {
            editor.value = data.text;
            btn.innerText = '[ 2. AI 识别目录 ]';
        } else {
            console.error('[错误] OCR 失败:', data);
            editor.value = `// 识别失败\n// 原因：${data.message}\n`;
            btn.innerText = '[ 重试识别 ]';
        }
    } catch (err) {
        console.error('[错误] 网络失败:', err);
        editor.value = `// 网络错误\n// 详情：${err.message}\n`;
        btn.innerText = '[ 重试识别 ]';
    } finally {
        btn.disabled = false;
    }
});

document.getElementById('generatePdfBtn').addEventListener('click', async () => {
    if (!currentFile) {
        alert('错误：请先上传 PDF。');
        return;
    }

    const tocText = document.getElementById('tocEditor').value;
    const tocStartPageValue = document.getElementById('baseOffset').value || 1;
    const tocStartPage = Math.max(1, parseInt(tocStartPageValue, 10) || 1);

    if (!tocText.trim()) {
        alert('错误：目录文本为空。');
        return;
    }

    const btn = document.getElementById('generatePdfBtn');
    btn.innerText = '[ 生成中... ]';
    btn.disabled = true;

    const formData = new FormData();
    formData.append('file', currentFile);
    formData.append('toc_text', tocText);
    formData.append('toc_start_page', String(tocStartPage));
    // Backward compatibility for old server versions.
    formData.append('base_offset', String(tocStartPage - 1));

    try {
        const response = await fetch(withPrefix('/api/generate_pdf'), {
            method: 'POST',
            body: formData
        });

        const contentType = response.headers.get('content-type') || '';
        if (!response.ok) {
            const text = await response.text();
            let message = `HTTP ${response.status}`;
            try {
                const data = JSON.parse(text);
                message = data.message ? `${message} - ${data.message}` : `${message} - ${text}`;
            } catch {
                message = `${message} - ${text}`;
            }
            throw new Error(message.slice(0, 400));
        }

        if (contentType.includes('application/json')) {
            const data = await response.json();
            if (data.status === 'error') {
                throw new Error(data.message || '后端返回错误');
            }
            throw new Error('后端返回了 JSON，而不是 PDF 文件');
        }

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = `目录_${currentFile.name}`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
    } catch (err) {
        console.error('[错误]', err);
        alert(`错误：生成 PDF 失败。\n${err.message || err}`);
    } finally {
        btn.innerText = '[ 3. 生成最终 PDF ]';
        btn.disabled = false;
    }
});

document.getElementById('splitBtn').addEventListener('click', async () => {
    if (!currentFile) {
        alert('错误：请先上传 PDF。');
        return;
    }

    const startPage = document.getElementById('splitStart').value;
    const endPage = document.getElementById('splitEnd').value;

    const btn = document.getElementById('splitBtn');
    btn.innerText = '[ 提取中... ]';
    btn.disabled = true;

    const formData = new FormData();
    formData.append('file', currentFile);
    formData.append('start_page', startPage);
    formData.append('end_page', endPage);

    try {
        const response = await fetch(withPrefix('/api/split_pdf'), {
            method: 'POST',
            body: formData
        });

        const contentType = response.headers.get('content-type') || '';
        if (!response.ok) {
            const text = await response.text();
            let message = `HTTP ${response.status}`;
            try {
                const data = JSON.parse(text);
                message = data.message ? `${message} - ${data.message}` : `${message} - ${text}`;
            } catch {
                message = `${message} - ${text}`;
            }
            throw new Error(message.slice(0, 400));
        }

        if (contentType.includes('application/json')) {
            const data = await response.json();
            if (data.status === 'error') {
                throw new Error(data.message || '后端返回错误');
            }
            throw new Error('后端返回了 JSON，而不是 PDF 文件');
        }

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = `拆分_${startPage}_${endPage}_${currentFile.name}`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
    } catch (err) {
        console.error('[错误]', err);
        alert(`错误：提取页面失败。\n${err.message || err}`);
    } finally {
        btn.innerText = '[ 提取页面 ]';
        btn.disabled = false;
    }
});
