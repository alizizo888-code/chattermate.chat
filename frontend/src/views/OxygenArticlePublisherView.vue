<script setup lang="ts">
import { onMounted, ref } from 'vue'
import api from '@/services/api'

const sites = ref<any[]>([])
const campaigns = ref<any[]>([])
const targets = ref<any[]>([])
const articles = ref<any[]>([])
const message = ref('')
const loading = ref(false)

const siteForm = ref({ name: 'Oxygen 11 WordPress', base_url: '', username: '', app_password: '', verify_ssl: true })
const campaignForm = ref({
  site_id: 0, name: 'Oxygen 11 Daily Articles', enabled: false, daily_count: 15,
  schedule_start_hour: 8, schedule_end_hour: 22, timezone: 'Asia/Riyadh',
  target_mode: 'fixed', target_page_id: null as number | null, post_type: 'page',
  category_ids: [] as number[], topics: [] as string[], keywords: [] as string[],
  content_mode: 'fresh', publish_status: 'draft', language: 'ar', article_style: 'standard', brand_instructions: '',
})
const topicText = ref('')
const keywordText = ref('')

async function load() {
  const [s, c] = await Promise.all([api.get('/article-publisher/sites'), api.get('/article-publisher/campaigns')])
  sites.value = s.data.sites
  campaigns.value = c.data.campaigns
  if (!campaignForm.value.site_id && sites.value[0]) campaignForm.value.site_id = sites.value[0].id
  if (campaignForm.value.site_id) await loadTargets()
}
async function loadTargets() {
  if (!campaignForm.value.site_id) return
  const r = await api.get('/article-publisher/sites/' + campaignForm.value.site_id + '/targets', { params: { target_type: campaignForm.value.post_type } })
  targets.value = r.data.targets
}
async function addSite() {
  loading.value = true; message.value = ''
  try { await api.post('/article-publisher/sites', siteForm.value); siteForm.value.app_password = ''; await load(); message.value = 'تم ربط موقع WordPress.' }
  catch (e: any) { message.value = e?.response?.data?.detail || 'فشل ربط الموقع.' }
  finally { loading.value = false }
}
async function addCampaign() {
  loading.value = true; message.value = ''
  try {
    campaignForm.value.topics = topicText.value.split('\n').map(x => x.trim()).filter(Boolean)
    campaignForm.value.keywords = keywordText.value.split(',').map(x => x.trim()).filter(Boolean)
    await api.post('/article-publisher/campaigns', campaignForm.value)
    await load(); message.value = 'تم حفظ حملة المقالات.'
  } catch (e: any) { message.value = e?.response?.data?.detail || 'فشل حفظ الحملة.' }
  finally { loading.value = false }
}
async function toggleCampaign(c: any) { await api.patch('/article-publisher/campaigns/' + c.id, { enabled: !c.enabled }); await load() }
async function runNow(c: any) {
  loading.value = true; message.value = 'جاري توليد ونشر المقال...'
  try { await api.post('/article-publisher/campaigns/' + c.id + '/run-now'); message.value = 'تم تنفيذ المقال.'; await loadArticles(c.id) }
  catch (e: any) { message.value = e?.response?.data?.detail || 'فشل التنفيذ.' }
  finally { loading.value = false }
}
async function loadArticles(id: number) {
  const r = await api.get('/article-publisher/campaigns/' + id + '/articles')
  articles.value = r.data.articles
}
onMounted(load)
</script>

<template>
  <div class="min-h-screen bg-slate-50 p-6 text-slate-900">
    <div class="mx-auto max-w-7xl space-y-6">
      <div>
        <h1 class="text-2xl font-bold">Oxygen 11 — AI Article Publisher</h1>
        <p class="mt-1 text-sm text-slate-600">توليد وجدولة ونشر المقالات على WordPress من نفس لوحة ChatterMate.</p>
      </div>
      <div v-if="message" class="rounded-lg border bg-white p-3 text-sm">{{ message }}</div>

      <section class="grid gap-6 lg:grid-cols-2">
        <div class="rounded-xl border bg-white p-5 shadow-sm">
          <h2 class="mb-4 font-semibold">ربط WordPress</h2>
          <div class="grid gap-3">
            <input v-model="siteForm.name" class="rounded border p-2" placeholder="اسم الموقع" />
            <input v-model="siteForm.base_url" class="rounded border p-2" placeholder="https://example.com" />
            <input v-model="siteForm.username" class="rounded border p-2" placeholder="WordPress username" />
            <input v-model="siteForm.app_password" type="password" class="rounded border p-2" placeholder="Application Password" />
            <button :disabled="loading" @click="addSite" class="rounded bg-slate-900 px-4 py-2 text-white">ربط الموقع</button>
          </div>
          <div class="mt-4 space-y-2 text-sm">
            <div v-for="s in sites" :key="s.id" class="rounded border p-3"><b>{{ s.name }}</b><div class="text-slate-500">{{ s.base_url }}</div></div>
          </div>
        </div>

        <div class="rounded-xl border bg-white p-5 shadow-sm">
          <h2 class="mb-4 font-semibold">إعداد حملة يومية</h2>
          <div class="grid gap-3">
            <select v-model="campaignForm.site_id" @change="loadTargets" class="rounded border p-2">
              <option :value="0">اختر الموقع</option><option v-for="s in sites" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
            <input v-model="campaignForm.name" class="rounded border p-2" placeholder="اسم الحملة" />
            <label class="text-sm">عدد المقالات يوميًا: {{ campaignForm.daily_count }} (1–30)</label>
            <input v-model.number="campaignForm.daily_count" type="range" min="1" max="30" />
            <div class="grid grid-cols-2 gap-3">
              <input v-model.number="campaignForm.schedule_start_hour" type="number" min="0" max="23" class="rounded border p-2" />
              <input v-model.number="campaignForm.schedule_end_hour" type="number" min="0" max="23" class="rounded border p-2" />
            </div>
            <input v-model="campaignForm.timezone" class="rounded border p-2" placeholder="Asia/Riyadh" />
            <select v-model="campaignForm.post_type" @change="loadTargets" class="rounded border p-2"><option value="page">Pages</option><option value="post">Posts</option></select>
            <select v-model="campaignForm.target_mode" class="rounded border p-2"><option value="fixed">نشر على الهدف المحدد</option><option value="random">توزيع/تدوير عشوائي على الأهداف</option></select>
            <select v-if="campaignForm.target_mode === 'fixed'" v-model="campaignForm.target_page_id" class="rounded border p-2">
              <option :value="null">اختر الصفحة</option><option v-for="t in targets" :key="t.id" :value="t.id">{{ t.title }} (#{{ t.id }})</option>
            </select>
            <select v-model="campaignForm.article_style" class="rounded border p-2"><option value="standard">تصميم قياسي</option><option value="how_to">دليل خطوة بخطوة</option><option value="service">مقال خدمة</option><option value="comparison">مقارنة</option><option value="faq">أسئلة شائعة</option><option value="listicle">قائمة/نصائح</option></select>
            <select v-model="campaignForm.content_mode" class="rounded border p-2"><option value="fresh">توليف جديد بالكامل</option><option value="rewrite">إعادة صياغة وتوليف من مقال سابق</option><option value="reuse">إعادة استخدام المحتوى السابق كما هو</option></select>
            <select v-model="campaignForm.publish_status" class="rounded border p-2"><option value="draft">Draft — للمراجعة</option><option value="pending">Pending</option><option value="publish">Publish مباشرة</option></select>
            <textarea v-model="topicText" class="min-h-20 rounded border p-2" placeholder="موضوع لكل سطر"></textarea>
            <textarea v-model="keywordText" class="min-h-20 rounded border p-2" placeholder="الكلمات المفتاحية مفصولة بفواصل"></textarea>
            <textarea v-model="campaignForm.brand_instructions" class="min-h-20 rounded border p-2" placeholder="تعليمات الأسلوب والبراند"></textarea>
            <label class="flex items-center gap-2 text-sm"><input v-model="campaignForm.enabled" type="checkbox" /> تفعيل الجدولة</label>
            <button :disabled="loading || !campaignForm.site_id" @click="addCampaign" class="rounded bg-indigo-600 px-4 py-2 text-white">حفظ الحملة</button>
          </div>
        </div>
      </section>

      <section class="rounded-xl border bg-white shadow-sm">
        <div class="border-b p-5 font-semibold">الحملات</div>
        <div v-for="c in campaigns" :key="c.id" class="flex flex-wrap items-center justify-between gap-3 border-b p-4 last:border-0">
          <div><div class="font-medium">{{ c.name }}</div><div class="text-sm text-slate-500">{{ c.daily_count }} مقال/يوم · {{ c.schedule_start_hour }}:00–{{ c.schedule_end_hour }}:00 · {{ c.target_mode }}</div></div>
          <div class="flex gap-2"><button @click="toggleCampaign(c)" class="rounded border px-3 py-1.5">{{ c.enabled ? 'إيقاف' : 'تشغيل' }}</button><button @click="runNow(c)" class="rounded bg-slate-900 px-3 py-1.5 text-white">تشغيل الآن</button><button @click="loadArticles(c.id)" class="rounded border px-3 py-1.5">السجل</button></div>
        </div>
      </section>

      <section v-if="articles.length" class="rounded-xl border bg-white shadow-sm">
        <div class="border-b p-5 font-semibold">آخر المقالات</div>
        <div v-for="a in articles" :key="a.id" class="border-b p-4 last:border-0">
          <div class="flex justify-between gap-3"><b>{{ a.title || 'بدون عنوان' }}</b><span class="text-xs">{{ a.status }}</span></div>
          <div class="mt-1 text-sm text-slate-500">{{ a.primary_keyword }} · {{ a.wp_url || 'غير منشور' }}</div>
        </div>
      </section>
    </div>
  </div>
</template>
