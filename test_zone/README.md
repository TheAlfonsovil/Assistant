# test_zone

Vue frontend with Java 25 Spring Boot 4 backend, deployed as single SPA in Docker.

## Structure

- `backend/` - Java 25 Spring Boot 4 backend
- `frontend/` - Vue 3 frontend
- `docker/` - Docker configuration and launch script

## Running

```bash
cd docker
./launch.sh
```

Frontend: http://localhost:80
Backend: http://localhost:8080
API: http://localhost:80/api/hello