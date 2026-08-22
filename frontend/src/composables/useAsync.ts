import { ref, type Ref } from 'vue'
import { errorMessage } from '@/api/client'

export interface AsyncState<T> {
  data: Ref<T | null>
  loading: Ref<boolean>
  error: Ref<string | null>
  execute: () => Promise<T | null>
}

export function useAsync<T>(loader: () => Promise<T>): AsyncState<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref<string | null>(null)

  const execute = async (): Promise<T | null> => {
    loading.value = true
    error.value = null
    try {
      const result = await loader()
      data.value = result
      return result
    } catch (cause) {
      error.value = errorMessage(cause)
      return null
    } finally {
      loading.value = false
    }
  }

  return { data, loading, error, execute }
}
