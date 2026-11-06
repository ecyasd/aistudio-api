<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import type { AccountWithStats } from '@/types/accounts.ts'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import ModelQuotaGrid from './ModelQuotaGrid.vue'
import {
  CheckCircle2,
  Clock,
  Trash2,
  Edit2,
  Layers,
  RotateCcw,
  AlertCircle,
  RefreshCw,
  Mail,
  Tag,
  Check,
} from 'lucide-vue-next'
import { useAccountsStore } from '@/stores/accounts.ts'

const props = defineProps<{
  account: AccountWithStats
  active: boolean
  activating?: boolean
  forceExpanded?: boolean
}>()

const emit = defineEmits<{
  activate: []
  delete: []
  editName: []
  clearCooldown: []
  clearModel: [model: string]
}>()

const modelsExpanded = ref(false)

watch(
  () => props.forceExpanded,
  (val) => {
    if (val !== undefined) {
      modelsExpanded.value = val
    }
  },
  { immediate: true },
)

const todayRequests = computed(() => {
  const reqs = props.account.model_requests || {}
  return Object.values(reqs).reduce((sum, count) => sum + (Number(count) || 0), 0)
})

const accountsStore = useAccountsStore()
const detectingEmail = ref(false)
const detectingTier = ref(false)
const togglingEnabled = ref(false)
const editingGroup = ref(false)
const groupInput = ref('')

const accountTier = computed(() => {
  return props.account.tier || 'Free'
})

const isEnabled = computed(() => {
  return props.account.enabled !== false
})

async function handleToggleEnabled() {
  togglingEnabled.value = true
  try {
    await accountsStore.toggleAccountEnabled(props.account.id)
  } finally {
    togglingEnabled.value = false
  }
}

function startEditGroup() {
  groupInput.value = props.account.group || 'default'
  editingGroup.value = true
}

async function saveGroup() {
  if (!editingGroup.value) return
  const target = groupInput.value.trim() || 'default'
  editingGroup.value = false
  if (target !== (props.account.group || 'default')) {
    await accountsStore.updateAccountGroup(props.account.id, target)
  }
}

async function handleDetectEmail() {
  detectingEmail.value = true
  try {
    await accountsStore.detectAccountEmail(props.account.id)
  } finally {
    detectingEmail.value = false
  }
}

async function handleDetectTier() {
  detectingTier.value = true
  try {
    await accountsStore.detectAccountTier(props.account.id)
  } finally {
    detectingTier.value = false
  }
}
</script>

<template>
  <div
    class="relative bg-panel border rounded-xl p-3  transition-all "
    :class="[
      !isEnabled
        ? 'border-line bg-canvas/60 opacity-60'
        : account.session_expired
          ? 'border-danger-line bg-danger-soft/20'
          : active
            ? 'border-accent-line bg-accent-soft/20 ring-1 ring-accent/20'
            : 'border-line/80 hover:border-line-strong',
    ]"
  >
    <!-- Sub-Account Header Bar -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
      <div class="flex items-center gap-2.5 min-w-0">
        <!-- Enable/Disable Switch -->
        <button
          type="button"
          role="switch"
          :aria-checked="isEnabled"
          :disabled="togglingEnabled"
          class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none"
          :class="[isEnabled ? 'bg-success' : 'bg-line-strong', { 'opacity-50 cursor-wait': togglingEnabled }]"
          title="启用或禁用此账号参与调度"
          @click="handleToggleEnabled"
        >
          <span
            class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-panel  ring-0 transition duration-200 ease-in-out"
            :class="isEnabled ? 'translate-x-4' : 'translate-x-0'"
          />
        </button>

        <!-- User Index Badge: u/0, u/1... -->
        <span
          class="px-2 py-0.5 rounded-full text-xs font-mono font-semibold shrink-0"
          :class="[
            !isEnabled
              ? 'bg-line text-muted'
              : account.session_expired
                ? 'bg-danger-soft text-danger border border-danger-line'
                : active
                  ? 'bg-accent text-on-accent'
                  : 'bg-info-soft text-info border border-info-line/60',
          ]"
        >
          u/{{ account.auth_user }}
        </span>

        <!-- Benefit Tier Badge: Free / Plus / Pro / Ultra -->
        <button
          type="button"
          class="px-2 py-0.5 rounded-full text-[11px] font-mono font-semibold shrink-0 transition-opacity hover:opacity-80 cursor-pointer"
          :class="[
            accountTier === 'Ultra'
              ? 'bg-violet-soft text-violet border border-violet-line'
              : accountTier === 'Pro'
                ? 'bg-warning-soft text-warning border border-warning-line'
                : accountTier === 'Plus'
                  ? 'bg-success-soft text-success border border-success-line'
                  : 'bg-raised text-secondary border border-line/80',
            { 'opacity-50 cursor-wait': detectingTier },
          ]"
          :disabled="detectingTier"
          title="点击重新探测此子账号的订阅类型 (Free / Plus / Pro / Ultra)"
          @click="handleDetectTier"
        >
          {{ detectingTier ? '...' : accountTier }}
        </button>

        <!-- Group Tag / Inline Editor -->
        <div class="flex items-center">
          <div
            v-if="editingGroup"
            class="flex items-center gap-1"
          >
            <input
              v-model="groupInput"
              type="text"
              class="px-1.5 py-0.5 text-[11px] font-mono border rounded focus:outline-none focus:ring-1 focus:ring-accent w-20"
              placeholder="group"
              @keydown.enter="saveGroup"
              @blur="saveGroup"
            >
            <button
              type="button"
              class="text-success hover:text-success"
              @click="saveGroup"
            >
              <Check class="w-3 h-3" />
            </button>
          </div>
          <button
            v-else
            type="button"
            class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-mono font-medium text-muted bg-raised hover:bg-line/80 transition-colors cursor-pointer border border-line"
            title="点击修改所属分组"
            @click="startEditGroup"
          >
            <Tag class="w-2.5 h-2.5 text-muted" />
            <span>{{ account.group || 'default' }}</span>
          </button>
        </div>

        <!-- Account Name & Memo -->
        <div class="truncate">
          <div class="flex items-center gap-1.5">
            <span class="font-semibold text-fg text-xs truncate">
              {{ account.email || account.name || 'Google Account' }}
            </span>
            <button
              type="button"
              class="text-fg hover:text-secondary transition-colors cursor-pointer"
              title="重命名账号"
              @click="emit('editName')"
            >
              <Edit2 class="w-3 h-3" />
            </button>
          </div>
          <div class="text-[11px] text-muted font-mono truncate flex items-center gap-1.5">
            <span
              v-if="account.email && account.name && account.name !== account.email"
              class="text-muted"
            >{{ account.name }} ·</span>
            <span
              v-else-if="!account.email"
              class="text-warning/90 bg-warning-soft px-1 py-0.2 rounded text-[10px] border border-warning-line/50"
            >未记录邮箱</span>
            <span>ID: {{ account.id }}</span>
          </div>
        </div>
      </div>

      <!-- Sub-Account Stats & Controls -->
      <div class="flex items-center gap-2 sm:gap-3 shrink-0 flex-wrap">
        <div class="flex items-center gap-2 text-xs font-mono">
          <span class="text-secondary">今日: <strong>{{ todayRequests }}</strong></span>
          <span class="text-muted">· 累计: <strong>{{ account.requests || 0 }}</strong></span>
          <Badge
            variant="green"
            size="sm"
          >
            {{ account.success || 0 }}
          </Badge>
          <Badge
            :variant="(account.rate_limited || 0) > 0 ? 'red' : 'gray'"
            size="sm"
          >
            429: {{ account.rate_limited || 0 }}
          </Badge>
        </div>

        <!-- Status Pill -->
        <div
          v-if="!isEnabled"
          class="inline-flex items-center gap-1 text-[11px] font-semibold text-muted bg-raised px-2 py-0.5 rounded-full border border-line"
        >
          <span>已停用</span>
        </div>
        <div
          v-else-if="account.session_expired"
          class="inline-flex items-center gap-1.5 text-[11px] font-semibold text-danger bg-danger-soft px-2 py-0.5 rounded-full border border-danger-line"
          title="该账号登录态已失效，已被系统禁用。更新 Cookie 凭据后可点击恢复。"
        >
          <AlertCircle class="w-3 h-3 text-danger" />
          <span>登录态失效</span>
        </div>
        <div
          v-else-if="active"
          class="inline-flex items-center gap-1 text-[11px] font-semibold text-success bg-success-soft px-2 py-0.5 rounded-full border border-success-line"
        >
          <CheckCircle2 class="w-3 h-3" />
          <span>当前激活</span>
        </div>
        <div
          v-else-if="account.is_available === false"
          class="inline-flex items-center gap-1 text-[11px] font-semibold text-warning bg-warning-soft px-2 py-0.5 rounded-full border border-warning-line"
        >
          <Clock class="w-3 h-3" />
          <span>配额耗尽</span>
        </div>
        <div
          v-else
          class="text-[11px] text-muted font-medium"
        >
          就绪
        </div>

        <!-- Action Buttons -->
        <div class="flex items-center gap-1.5 ml-1">
          <!-- 恢复按钮 -->
          <button
            v-if="account.session_expired"
            type="button"
            class="px-2 py-0.5 text-[11px] font-medium text-danger bg-danger-soft hover:bg-danger-soft rounded-lg transition-colors cursor-pointer flex items-center gap-1"
            title="解除登录态失效禁用状态并重新测试"
            @click="emit('clearCooldown')"
          >
            <RefreshCw class="w-3 h-3" />
            <span>恢复</span>
          </button>

          <Button
            v-if="!active && !account.session_expired && isEnabled"
            variant="secondary"
            size="sm"
            :loading="activating"
            @click="emit('activate')"
          >
            <span>激活</span>
          </Button>

          <button
            type="button"
            class="p-1 text-muted hover:text-accent hover:bg-raised rounded-lg transition-colors cursor-pointer"
            :class="{ 'opacity-50 cursor-not-allowed': detectingEmail }"
            title="自动识别并记录此子账号真实邮箱与订阅类型"
            :disabled="detectingEmail"
            @click="handleDetectEmail"
          >
            <Mail class="w-3.5 h-3.5" />
          </button>

          <button
            v-if="(account.rate_limited || 0) > 0 && !account.session_expired"
            type="button"
            class="p-1 text-muted hover:text-warning hover:bg-warning-soft rounded-lg transition-colors cursor-pointer"
            title="清除该账号锁定"
            @click="emit('clearCooldown')"
          >
            <RotateCcw class="w-3.5 h-3.5" />
          </button>

          <button
            type="button"
            class="p-1 text-muted hover:text-accent hover:bg-raised rounded-lg transition-colors cursor-pointer"
            :class="{ 'text-accent bg-accent-soft': modelsExpanded }"
            title="展开/收起模型今日配额明细"
            @click="modelsExpanded = !modelsExpanded"
          >
            <Layers class="w-3.5 h-3.5" />
          </button>

          <button
            type="button"
            class="p-1 text-fg hover:text-danger hover:bg-danger-soft rounded-lg transition-colors cursor-pointer border-l border-line pl-1.5 ml-1"
            title="删除此子账号"
            @click="emit('delete')"
          >
            <Trash2 class="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>

    <!-- Level 3: Per-Model Independent Quota/Rate Limits -->
    <ModelQuotaGrid
      v-if="modelsExpanded"
      :account="account"
      @clear-model="emit('clearModel', $event)"
    />
  </div>
</template>
