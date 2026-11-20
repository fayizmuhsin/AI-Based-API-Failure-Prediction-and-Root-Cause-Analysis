const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api'

export async function fetchBackendHealth(signal) {
  let response
  try {
    response = await fetch(`${API_BASE_URL}/health`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal,
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new Error('Unable to reach the backend. Check that the API server is running.')
  }

  if (!response.ok) {
    throw new Error(`Backend returned HTTP ${response.status}.`)
  }
  return response.json()
}
