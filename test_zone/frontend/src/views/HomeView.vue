<template>
  <div class="home">
    <h2>Home</h2>
    <p>Welcome to test_zone</p>
    <button @click="fetchHello">Fetch Backend Message</button>
    <div v-if="message">
      <p>{{ message }}</p>
    </div>
  </div>
</template>

<script>
export default {
  name: 'HomeView',
  data() {
    return {
      message: ''
    }
  },
  methods: {
    async fetchHello() {
      try {
        const response = await fetch('/api/hello')
        const data = await response.json()
        this.message = data.message
      } catch (error) {
        this.message = 'Error: ' + error.message
      }
    }
  }
}
</script>