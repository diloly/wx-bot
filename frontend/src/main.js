import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import * as Icons from '@element-plus/icons-vue'
import App from './App.vue'
import './styles.css'

// 主题在挂载前就落到 <html> 上，避免首屏亮色闪一下
if (localStorage.getItem('wb-theme') === 'dark') {
  document.documentElement.classList.add('dark')
}

const app = createApp(App)
for (const [name, comp] of Object.entries(Icons)) app.component(name, comp)
app.use(ElementPlus)
app.mount('#app')
