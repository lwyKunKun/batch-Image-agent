<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import doubleTemplateImage from './assets/img/1.png'
import singleTemplateImage from './assets/img/2.jpg'

const MAX_PRODUCTS_PER_BATCH = 10
const API_CALLS_PER_IMAGE = 2
const uploadFields = [
  { key: 'jar', index: '01', title: '罐装主图', hint: '完整展示罐体、标签与罐内药材', keywords: ['罐', '瓶', '包装', '主图'] },
  { key: 'herb_overall', index: '02', title: '药材整体图', hint: '俯拍或成堆展示，用于还原外形', keywords: ['整体', '全景', '俯拍', '堆放'] },
  { key: 'herb_detail', index: '03', title: '药材细节图', hint: '近景展示切面、颜色与真实纹理', keywords: ['细节', '特写', '近景', '切面'] },
]
const allowedTypes = new Set(['image/png', 'image/jpeg', 'image/webp'])
const terminalStatuses = new Set(['completed', 'partial', 'failed'])
const templatePreviews = {
  single: singleTemplateImage,
  double: doubleTemplateImage,
}

const products = ref([])
const templates = ref([])
const selectedTemplates = ref(['single', 'double'])
const health = ref(null)
const jobs = ref([])
const processing = ref(false)
const started = ref(false)
const currentBatchIndex = ref(-1)
const pageError = ref('')
const importNotice = ref('')
let nextProductId = 1
let nextFileId = 1
let runToken = 0

const canSubmit = computed(() =>
  products.value.length > 0 &&
  products.value.every(isProductReady) &&
  selectedTemplates.value.length > 0 &&
  !processing.value,
)
const batchCount = computed(() => Math.ceil(products.value.length / MAX_PRODUCTS_PER_BATCH))
const expectedResults = computed(() => products.value.length * selectedTemplates.value.length)
const expectedApiCalls = computed(() => expectedResults.value * API_CALLS_PER_IMAGE)
const totalSteps = computed(() => expectedApiCalls.value)
const completedSteps = computed(() => jobs.value.reduce((sum, item) => sum + item.completed_steps, 0))
const progress = computed(() => {
  if (!totalSteps.value) return 0
  return Math.round((completedSteps.value / totalSteps.value) * 100)
})
const activeJob = computed(() => jobs.value[currentBatchIndex.value] || null)
const totalFailures = computed(() => jobs.value.reduce((sum, item) => sum + jobFailureCount(item), 0))

function validateFiles(fileList) {
  const accepted = []
  let unsupported = 0
  let oversized = 0
  Array.from(fileList || []).forEach((file) => {
    if (!allowedTypes.has(file.type)) unsupported += 1
    else if (file.size > 20 * 1024 * 1024) oversized += 1
    else accepted.push(file)
  })
  const messages = []
  if (unsupported) messages.push(`已忽略 ${unsupported} 个非 PNG、JPG 或 WebP 文件`)
  if (oversized) messages.push(`已忽略 ${oversized} 个超过 20MB 的文件`)
  return { accepted, messages }
}

function makeFileEntry(file) {
  return {
    id: `file-${nextFileId++}`,
    file,
    name: file.name,
    preview: URL.createObjectURL(file),
  }
}

function inferRoles(entries) {
  const roles = { jar: '', herb_overall: '', herb_detail: '' }
  uploadFields.forEach((field) => {
    const matches = entries.filter((entry) => field.keywords.some((keyword) => entry.name.toLowerCase().includes(keyword)))
    if (matches.length === 1 && !Object.values(roles).includes(matches[0].id)) roles[field.key] = matches[0].id
  })
  return roles
}

function deriveProductName(files) {
  const keywordPattern = /罐装主图|药材整体图|药材细节图|罐装|主图|整体|全景|俯拍|细节|特写|近景|切面/gi
  const names = files
    .map((file) => file.name.replace(/\.[^.]+$/, '').replace(keywordPattern, '').replace(/[-_\s]+/g, '').trim())
    .filter(Boolean)
  if (names.length && names.every((name) => name === names[0])) return names[0]
  return `产品 ${nextProductId}`
}

function addProduct(name, files) {
  const entries = files.map(makeFileEntry)
  products.value.push({
    id: `product-${nextProductId++}`,
    productName: name,
    candidates: entries,
    roles: inferRoles(entries),
  })
}

function onFilesSelected(event) {
  const { accepted, messages } = validateFiles(event.target.files)
  importNotice.value = messages.join('；')
  pageError.value = ''
  if (accepted.length !== 3) {
    pageError.value = `每个产品必须正好选择 3 张有效图片，本次识别到 ${accepted.length} 张。`
  } else {
    addProduct(deriveProductName(accepted), accepted)
  }
  event.target.value = ''
}

function onFolderSelected(event) {
  const { accepted, messages } = validateFiles(event.target.files)
  const groups = new Map()
  accepted.forEach((file) => {
    const parts = file.webkitRelativePath.split('/').filter(Boolean)
    const directoryParts = parts.slice(0, -1)
    const groupKey = directoryParts.join('/') || `未命名-${nextProductId}`
    const productName = directoryParts.at(-1) || `产品 ${nextProductId}`
    if (!groups.has(groupKey)) groups.set(groupKey, { productName, files: [] })
    groups.get(groupKey).files.push(file)
  })

  const invalidGroups = []
  let added = 0
  groups.forEach((group) => {
    if (group.files.length === 3) {
      addProduct(group.productName, group.files)
      added += 1
    } else {
      invalidGroups.push(`${group.productName}（${group.files.length} 张）`)
    }
  })

  if (added) messages.push(`已追加 ${added} 个产品文件夹`)
  if (invalidGroups.length) messages.push(`以下目录不是 3 张有效图片，已跳过：${invalidGroups.join('、')}`)
  importNotice.value = messages.join('；')
  pageError.value = added || !groups.size ? '' : '没有找到符合要求的产品目录。'
  event.target.value = ''
}

function selectedEntry(product, role) {
  return product.candidates.find((entry) => entry.id === product.roles[role])
}

function assignRole(product, role, fileId) {
  Object.keys(product.roles).forEach((otherRole) => {
    if (otherRole !== role && product.roles[otherRole] === fileId) product.roles[otherRole] = ''
  })
  product.roles[role] = fileId
}

function isUsedByOtherRole(product, role, fileId) {
  return Object.entries(product.roles).some(([otherRole, selectedId]) => otherRole !== role && selectedId === fileId)
}

function isProductReady(product) {
  const roleIds = uploadFields.map((field) => product.roles[field.key])
  return Boolean(product.productName.trim()) && product.candidates.length === 3 && roleIds.every(Boolean) && new Set(roleIds).size === roleIds.length
}

function productIssue(product) {
  if (!product.productName.trim()) return '请输入产品名称'
  if (product.candidates.length !== 3) return '每个产品必须正好包含 3 张图片'
  const missing = uploadFields.filter((field) => !product.roles[field.key]).map((field) => field.title)
  if (missing.length) return `待分配：${missing.join('、')}`
  return ''
}

function releaseProduct(product) {
  product.candidates.forEach((entry) => URL.revokeObjectURL(entry.preview))
}

function removeProduct(productId) {
  const index = products.value.findIndex((product) => product.id === productId)
  if (index < 0) return
  releaseProduct(products.value[index])
  products.value.splice(index, 1)
}

function toggleTemplate(id) {
  const index = selectedTemplates.value.indexOf(id)
  if (index >= 0) selectedTemplates.value.splice(index, 1)
  else selectedTemplates.value.push(id)
}

async function getJson(response) {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || '请求失败，请检查后端服务。')
  return data
}

async function loadInitialData() {
  try {
    const [healthResponse, templateResponse] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/templates'),
    ])
    health.value = await getJson(healthResponse)
    templates.value = (await getJson(templateResponse)).map((template) => ({
      ...template,
      image_url: templatePreviews[template.id] || template.image_url,
    }))
  } catch (error) {
    pageError.value = error.message
  }
}

function buildBatchForm(batchProducts) {
  const form = new FormData()
  const manifest = { products: [] }
  let uploadIndex = 0
  batchProducts.forEach((product) => {
    const item = { product_name: product.productName.trim() }
    uploadFields.forEach((field) => {
      const entry = selectedEntry(product, field.key)
      item[field.key] = uploadIndex++
      form.append('uploads', entry.file, entry.name)
    })
    manifest.products.push(item)
  })
  form.append('manifest', JSON.stringify(manifest))
  form.append('templates', selectedTemplates.value.join(','))
  return form
}

function splitBatches(items) {
  const batches = []
  for (let index = 0; index < items.length; index += MAX_PRODUCTS_PER_BATCH) {
    batches.push(items.slice(index, index + MAX_PRODUCTS_PER_BATCH))
  }
  return batches
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

async function pollJob(index, token) {
  while (token === runToken) {
    await wait(1200)
    if (token !== runToken) return null
    const updated = await getJson(await fetch(`/api/jobs/${jobs.value[index].id}`))
    jobs.value[index] = updated
    if (terminalStatuses.has(updated.status)) return updated
  }
  return null
}

async function submit() {
  if (!canSubmit.value) return
  const token = ++runToken
  const batches = splitBatches(products.value)
  processing.value = true
  started.value = true
  jobs.value = []
  pageError.value = ''

  try {
    for (let index = 0; index < batches.length; index += 1) {
      if (token !== runToken) return
      currentBatchIndex.value = index
      const created = await getJson(await fetch('/api/jobs', {
        method: 'POST',
        body: buildBatchForm(batches[index]),
      }))
      jobs.value.push(created)
      await pollJob(index, token)
    }
  } catch (error) {
    pageError.value = error.message
  } finally {
    if (token === runToken) processing.value = false
  }
}

async function retryFailures(jobIndex, items = null) {
  if (processing.value) return
  const token = ++runToken
  processing.value = true
  currentBatchIndex.value = jobIndex
  pageError.value = ''
  try {
    const updated = await getJson(await fetch(`/api/jobs/${jobs.value[jobIndex].id}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(items ? { items } : {}),
    }))
    jobs.value[jobIndex] = updated
    await pollJob(jobIndex, token)
  } catch (error) {
    pageError.value = error.message
  } finally {
    if (token === runToken) processing.value = false
  }
}

function productProgress(product) {
  if (!product.total_steps) return 0
  return Math.round((product.completed_steps / product.total_steps) * 100)
}

function jobFailureCount(item) {
  return item.products.reduce((sum, product) => sum + product.errors.length, 0)
}

function statusText(status) {
  return {
    queued: '等待中',
    running: '生成中',
    completed: '已完成',
    partial: '部分失败',
    failed: '失败',
  }[status] || status
}

function reset() {
  runToken += 1
  products.value.forEach(releaseProduct)
  products.value = []
  jobs.value = []
  processing.value = false
  started.value = false
  currentBatchIndex.value = -1
  pageError.value = ''
  importNotice.value = ''
}

onMounted(loadInitialData)
onBeforeUnmount(() => {
  runToken += 1
  products.value.forEach(releaseProduct)
})
</script>

<template>
  <div class="shell">
    <header class="hero">
      <nav class="nav">
        <div class="brand"><span class="seal">本草</span><strong>AI 主图工坊</strong></div>
        <div class="service-state" :class="{ ready: health?.api_key_configured }">
          <i></i>{{ health?.api_key_configured ? 'AI 服务就绪' : '等待服务配置' }}
        </div>
      </nav>
      <div class="hero-copy">
        <p class="eyebrow">BATCH HERBAL PRODUCT IMAGERY</p>
        <h1>整批素材，一次上传<br><em>批量生成电商场景图</em></h1>
        <p class="intro">每个产品严格准备罐装主图、药材整体图和细节图三张素材。超过 10 个产品时自动拆批，按顺序稳定生成。</p>
      </div>
      <div class="process-line" aria-label="生成流程">
        <span><b>1</b> 批量导入产品</span><i></i><span><b>2</b> 校正素材并选模板</span><i></i><span><b>3</b> 自动拆批生成</span>
      </div>
    </header>

    <main>
      <section class="workspace" v-if="!started">
        <div class="section-heading">
          <div><span class="section-number">01</span><h2>导入产品素材</h2></div>
          <p>每个产品必须正好包含三张有效图片</p>
        </div>

        <div class="import-actions">
          <label class="import-button primary">
            <input type="file" multiple accept="image/png,image/jpeg,image/webp" @change="onFilesSelected" />
            <strong>选择三张图片</strong>
            <small>本次选择作为一个产品</small>
          </label>
          <label class="import-button">
            <input type="file" multiple webkitdirectory directory @change="onFolderSelected" />
            <strong>选择产品文件夹</strong>
            <small>每个子文件夹必须包含三张图片</small>
          </label>
        </div>
        <p class="folder-example">目录示例：待上传目录 / 丹参片 / 罐装主图.jpg、药材整体.jpg、药材细节.jpg</p>
        <p v-if="importNotice" class="notice-message">{{ importNotice }}</p>
        <p v-if="pageError" class="error-message">{{ pageError }}</p>

        <div v-if="products.length" class="batch-summary">
          <strong>当前共 {{ products.length }} 个产品</strong>
          <span>{{ products.filter(isProductReady).length }} 个已就绪 · 将拆分为 {{ batchCount }} 批</span>
        </div>
        <div v-else class="empty-state">尚未添加产品。请选择三张图片，或一次选择包含多个产品子文件夹的根目录。</div>

        <div class="product-list">
          <article v-for="(product, productIndex) in products" :key="product.id" class="product-editor" :class="{ invalid: !isProductReady(product) }">
            <header class="product-editor-head">
              <span class="product-order">{{ String(productIndex + 1).padStart(2, '0') }}</span>
              <label>
                <span>产品名称</span>
                <input v-model="product.productName" maxlength="50" />
              </label>
              <button type="button" class="delete-product" @click="removeProduct(product.id)">删除产品</button>
            </header>

            <div class="role-grid">
              <div v-for="field in uploadFields" :key="field.key" class="role-picker">
                <div class="role-preview">
                  <img v-if="selectedEntry(product, field.key)" :src="selectedEntry(product, field.key).preview" :alt="field.title" />
                  <span v-else>{{ field.index }}</span>
                </div>
                <div class="role-control">
                  <strong>{{ field.title }}</strong>
                  <small>{{ field.hint }}</small>
                  <select :value="product.roles[field.key]" @change="assignRole(product, field.key, $event.target.value)">
                    <option value="">请选择对应图片</option>
                    <option
                      v-for="entry in product.candidates"
                      :key="entry.id"
                      :value="entry.id"
                      :disabled="isUsedByOtherRole(product, field.key, entry.id)"
                    >{{ entry.name }}</option>
                  </select>
                </div>
              </div>
            </div>
            <p v-if="productIssue(product)" class="product-warning">{{ productIssue(product) }}</p>
          </article>
        </div>

        <div class="section-heading template-heading">
          <div><span class="section-number">02</span><h2>选择效果模板</h2></div>
          <p>默认选择两种模板，并应用到每个产品</p>
        </div>
        <div class="template-grid">
          <button
            v-for="template in templates"
            :key="template.id"
            type="button"
            class="template-card"
            :class="{ selected: selectedTemplates.includes(template.id) }"
            @click="toggleTemplate(template.id)"
          >
            <img :src="template.image_url" :alt="template.name" />
            <span class="check">{{ selectedTemplates.includes(template.id) ? '✓' : '' }}</span>
            <span class="template-name"><strong>{{ template.name }}</strong><small>方形电商主图 · AI 场景替换</small></span>
          </button>
        </div>

        <div v-if="products.length" class="generation-estimate">
          <span><strong>{{ batchCount }}</strong> 个顺序批次</span>
          <span><strong>{{ expectedResults }}</strong> 张效果图</span>
          <span><strong>{{ expectedApiCalls }}</strong> 次 AI 调用</span>
          <small>服务端全局最多同时执行 2 个生成任务，上传原图不压缩</small>
        </div>

        <button class="generate-button" :disabled="!canSubmit" @click="submit">
          <span>开始批量 AI 生成</span><small>预计生成 {{ expectedResults }} 张效果图</small>
        </button>
      </section>

      <section class="status-panel" v-else-if="processing">
        <div class="loader"><span></span><span></span><span></span></div>
        <p class="eyebrow">BATCH GENERATING</p>
        <h2>正在处理第 {{ currentBatchIndex + 1 }} / {{ batchCount }} 批</h2>
        <p>{{ activeJob?.message || '正在创建批次任务' }}</p>
        <div class="progress-track"><i :style="{ width: `${progress}%` }"></i></div>
        <div class="progress-label"><span>{{ completedSteps }} / {{ totalSteps }} 次 AI 阶段</span><strong>{{ progress }}%</strong></div>
        <div v-if="activeJob" class="batch-status-list">
          <div v-for="product in activeJob.products" :key="`${activeJob.id}-${product.id}`" class="batch-status-row">
            <div><strong>{{ product.product_name }}</strong><small>{{ product.message }}</small></div>
            <span :class="`status-${product.status}`">{{ statusText(product.status) }} · {{ productProgress(product) }}%</span>
          </div>
        </div>
        <p v-if="pageError" class="error-message">{{ pageError }}</p>
      </section>

      <section class="results" v-else>
        <p class="eyebrow">BATCH COMPLETE</p>
        <h2>{{ totalFailures ? `生成完成，${totalFailures} 张图片失败` : '全部效果图已生成' }}</h2>
        <p>共处理 {{ products.length }} 个产品、{{ jobs.length }} 个批次，成功结果不会在重试时重复生成。</p>
        <p v-if="pageError" class="error-message">{{ pageError }}</p>

        <section v-for="(batchJob, batchIndex) in jobs" :key="batchJob.id" class="result-batch">
          <header class="result-batch-head">
            <div>
              <span>第 {{ batchIndex + 1 }} 批</span>
              <strong>{{ statusText(batchJob.status) }}</strong>
            </div>
            <button
              v-if="jobFailureCount(batchJob)"
              type="button"
              class="retry-batch-button"
              :disabled="processing"
              @click="retryFailures(batchIndex)"
            >重试本批 {{ jobFailureCount(batchJob) }} 个失败项</button>
          </header>

          <article v-for="product in batchJob.products" :key="`${batchJob.id}-${product.id}`" class="result-product">
            <header>
              <h3>{{ product.product_name }}</h3>
              <span :class="`status-${product.status}`">{{ statusText(product.status) }}</span>
            </header>
            <div v-if="product.results.length" class="result-grid">
              <article v-for="result in product.results" :key="result.template_id">
                <a :href="result.image_url" target="_blank"><img :src="result.image_url" :alt="result.name" /></a>
                <div><strong>{{ result.name }}</strong><a :href="result.download_url">下载原图 ↓</a></div>
              </article>
            </div>
            <ul v-if="product.errors.length" class="result-errors">
              <li v-for="error in product.errors" :key="error.template_id">
                <span><strong>{{ error.name }}：</strong>{{ error.message }}</span>
                <button
                  type="button"
                  :disabled="processing"
                  @click="retryFailures(batchIndex, [{ product_id: product.id, template_id: error.template_id }])"
                >重试此图</button>
              </li>
            </ul>
          </article>
        </section>
        <button class="again-button" @click="reset">继续生成下一批</button>
      </section>
    </main>

    <footer>本草造物 · Qwen Image Edit 驱动 · 图片仅保存在本机</footer>
  </div>
</template>
